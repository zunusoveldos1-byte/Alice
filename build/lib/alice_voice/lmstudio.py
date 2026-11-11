"""OpenAI-compatible LM Studio adapter; shares the runtime's HTTP pool.

Tools are an explicit allowlist supplied by the application, never model code.
No default computer actions or implicit screenshot capture are registered.
"""
import asyncio
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
import json
import re

import httpx
from .secrets import load_lmstudio_token, MissingCredentialError


def error(message):
    # Local import keeps the common facade/error interface without a cycle.
    from .providers import ProviderError
    return ProviderError('LM Studio: ' + message)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    execute: Callable[[dict], Awaitable[object]]


def validate_arguments(value, schema, depth=0):
    """Deliberately small, closed JSON Schema subset for trusted registrations."""
    if depth > 12 or set(schema) - {'type', 'properties', 'required', 'additionalProperties',
                                     'items', 'enum', 'description', 'minimum', 'maximum', 'maxLength'}:
        raise ValueError('unsupported schema')
    kind = schema.get('type')
    types = {'object': dict, 'array': list, 'string': str, 'integer': int,
             'boolean': bool, 'null': type(None)}
    if kind == 'number':
        valid = type(value) in (int, float)
    else:
        valid = kind in types and type(value) is types[kind]
    if not valid or ('enum' in schema and value not in schema['enum']):
        raise ValueError('invalid argument type')
    if kind == 'object':
        properties = schema.get('properties', {})
        if set(value) - set(properties) or not set(schema.get('required', [])) <= set(value):
            raise ValueError('invalid argument keys')
        for key, item in value.items():
            validate_arguments(item, properties[key], depth + 1)
    elif kind == 'array':
        if len(value) > 128 or 'items' not in schema:
            raise ValueError('invalid array')
        for item in value:
            validate_arguments(item, schema['items'], depth + 1)
    elif kind == 'string' and len(value) > min(16000, schema.get('maxLength', 16000)):
        raise ValueError('argument too long')
    elif kind in ('number', 'integer'):
        import math
        if not math.isfinite(value) or value < schema.get('minimum', -float('inf')) or value > schema.get('maximum', float('inf')):
            raise ValueError('invalid number')


class SpeechText:
    """Suppress explicit reasoning/tool markup even when tags straddle deltas."""
    pairs = {'<think>': '</think>', '<thinking>': '</thinking>', '<analysis>': '</analysis>',
             '<tool_call>': '</tool_call>', '[think]': '[/think]'}

    def __init__(self):
        self.pending = ''
        self.hidden = []

    def feed(self, text, final=False):
        self.pending += text
        out = []
        while self.pending:
            lower = self.pending.lower()
            tokens = tuple(self.pairs) + ((self.hidden[-1],) if self.hidden else ())
            matches = [(lower.find(t), t) for t in tokens if t in lower]
            if matches:
                pos, tag = min(matches)
                if tag in self.pairs:
                    if not self.hidden:
                        out.append(self.pending[:pos])
                    if len(self.hidden) >= 32:
                        raise ValueError('reasoning nesting too deep')
                    self.hidden.append(self.pairs[tag])
                else:
                    self.hidden.pop()
                self.pending = self.pending[pos + len(tag):]
                continue
            keep = max((n for t in tokens for n in range(1, min(len(t), len(lower)) + 1)
                        if lower.endswith(t[:n])), default=0)
            take = len(self.pending) - keep
            if not self.hidden:
                out.append(self.pending[:take])
            self.pending = self.pending[take:]
            break
        if final:
            # Never expose unfinished tags or unclosed reasoning.
            self.pending = ''
        return ''.join(out)


class LMStudioProvider:
    def __init__(self, settings, client, token_loader=None, tools=()):
        self.settings, self.client = settings, client
        self.token_loader = token_loader or load_lmstudio_token
        self.tools = {t.name: t for t in tools}
        if len(self.tools) != len(tools) or any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,63}', n) for n in self.tools):
            raise ValueError('Invalid tool registration')

    def headers(self):
        try:
            token = self.token_loader()
        except MissingCredentialError:
            return {}
        except Exception:
            raise error('не удалось прочитать токен из Windows Credential Manager. Проверьте хранилище Windows.') from None
        if not isinstance(token, str) or not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise error('неверный токен. Замените или удалите его в настройках.')
        return {'Authorization': 'Bearer ' + token}

    def check_status(self, response, *, models=False):
        code = response.status_code
        if code == 200:
            return
        if code in (401, 403):
            raise error(f'ошибка авторизации (HTTP {code}). Добавьте или замените токен в настройках.')
        if code == 404:
            raise error('адрес API не найден. Проверьте URL сервера с окончанием /v1.' if models else
                        'модель недоступна (HTTP 404). Загрузите её в LM Studio или выберите ID из списка моделей.')
        if code in (502, 503, 504):
            raise error(f'сервер временно недоступен (HTTP {code}). Проверьте сервер и загрузку модели.')
        raise error(f'запрос отклонён (HTTP {code}). Проверьте модель, настройки сервера и поддержку инструментов.')

    def network_error(self, exc):
        if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
            return error('истёк таймаут. Проверьте загрузку модели и сервер.')
        return error('сервер недоступен. В LM Studio откройте Developer → Start Server; '
                     'в Bionic — Settings → Local Model API → Local API Server. '
                     'Проверьте адрес и порт в настройках Alice Voice.')

    async def models(self):
        try:
            async with asyncio.timeout(self.settings.timeout_s):
                async with self.client.stream('GET', self.settings.lmstudio_url.rstrip('/') + '/models',
                                              headers=self.headers()) as r:
                    self.check_status(r, models=True)
                    body = bytearray()
                    async for chunk in r.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 1024 * 1024:
                            raise error('слишком большой список моделей.')
                data = json.loads(body)
                ids = [m['id'] for m in data['data']]
                if any(not isinstance(n, str) or not n.strip() or len(n) > 512 or any(ord(c) < 32 for c in n) for n in ids):
                    raise ValueError
                return list(dict.fromkeys(ids))
        except (TimeoutError, httpx.RequestError) as exc:
            raise self.network_error(exc) from None
        except (ValueError, TypeError, KeyError):
            raise error('неверный формат списка моделей.') from None

    async def warmup(self):
        ids = await self.models()
        if not self.settings.lmstudio_model or self.settings.lmstudio_model not in ids:
            raise error('выбранная модель недоступна. Выберите фактический ID в настройках.')
        # Metadata/connection warmup only: no hidden generation or model change.

    async def probe_text(self):
        """Verify real text generation independently of tools and vision."""
        from dataclasses import replace
        adapter = LMStudioProvider(replace(self.settings, max_tokens=512, max_response_chars=500),
                                   self.client, self.token_loader)
        async with asyncio.timeout(self.settings.timeout_s):
            async with aclosing(adapter.stream([{'role': 'user', 'content':
                    'Коротко поздоровайся на русском языке.'}], lambda: None)) as stream:
                reply = ''.join([part async for part in stream]).strip()
        if not reply:
            raise error('модель не вернула текст. Загрузите её в LM Studio и повторите проверку.')
        return reply

    async def probe_tools(self):
        """One harmless model-specific protocol probe, never application tools."""
        from dataclasses import replace
        called = []
        async def acknowledge(args):
            called.append(args)
            return {'ok': True}
        probe = Tool('alice_capability_check', 'Connection check; has no external effects.',
                     {'type': 'object', 'properties': {'value': {'type': 'string', 'enum': ['alice-check']}},
                      'required': ['value'], 'additionalProperties': False}, acknowledge)
        adapter = LMStudioProvider(replace(self.settings, max_tokens=128, max_response_chars=1200),
                                   self.client, self.token_loader, [probe])
        adapter._probe = True
        async with asyncio.timeout(min(10, self.settings.timeout_s)):
            async with aclosing(adapter.stream([{'role': 'user', 'content':
                    'Call alice_capability_check with value alice-check. Do not answer with text.'}], lambda: None)) as stream:
                async for _ in stream:
                    pass
        return len(called) == 1

    async def events(self, response):
        pending, event = '', []
        size = 0
        async for line in response.aiter_lines():
            size += len(line)
            if len(line) > 65536 or size > 8 * 1024 * 1024:
                raise error('слишком большой поток ответа.')
            if line.startswith('data:'):
                event.append(line[5:].lstrip())
                if sum(map(len, event)) > 65536:
                    raise error('слишком большой чанк ответа.')
            elif not line and event:
                pending = '\n'.join(event)
                event.clear()
                yield pending
        if event:
            yield '\n'.join(event)

    async def stream(self, messages, first_token):
        try:
            async with aclosing(self._stream(messages, first_token)) as stream:
                async for text in stream:
                    yield text
        except (TimeoutError, httpx.RequestError) as exc:
            raise self.network_error(exc) from None
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            raise error('неверный или повреждённый поток ответа. Инструменты не выполнены.') from None

    async def _stream(self, messages, first_token):
        s = self.settings
        if not s.lmstudio_model:
            raise error('выберите модель в настройках и проверьте подключение.')
        history = [{'role': 'system', 'content': s.system_prompt}] + list(messages)
        # Official Qwen3 soft switch. Do not guess server-specific JSON fields or
        # apply this to Qwen3.5 / thinking-only variants that cannot disable it.
        if re.search(r'(?:^|/)qwen3[-_:](?:0\.6|1\.7|4|8|14|30|32|235)b(?:[-_:.]|$)', s.lmstudio_model, re.I):
            for i in range(len(history) - 1, 0, -1):
                if history[i].get('role') == 'user':
                    history[i] = dict(history[i], content=history[i]['content'] + '\n/no_think')
                    break
        total, first = 0, True
        for round_index in range(5):
            self.check_cancelled()
            body = {'model': s.lmstudio_model, 'messages': history, 'stream': True, 'max_tokens': s.max_tokens}
            if self.tools:
                body['tools'] = [{'type': 'function', 'function': {'name': t.name, 'description': t.description,
                                  'parameters': t.parameters}} for t in self.tools.values()]
                body['tool_choice'] = 'auto'
            calls, content, finish = {}, '', None
            speech = SpeechText()
            async with self.client.stream('POST', s.lmstudio_url.rstrip('/') + '/chat/completions',
                    headers=self.headers(), json=body) as response:
                self.check_status(response)
                async for payload in self.events(response):
                    self.check_cancelled()
                    if payload == '[DONE]':
                        break
                    data = json.loads(payload)
                    if 'error' in data:
                        raise error('сервер сообщил об ошибке генерации. Проверьте выбранную модель.')
                    choices = data.get('choices', [])
                    if not choices:
                        continue  # Optional usage-only chunk.
                    choice = choices[0]
                    if choice.get('index', 0) != 0 or finish is not None:
                        raise ValueError('unexpected stream choice')
                    delta = choice.get('delta', {})
                    for part in delta.get('tool_calls', []):
                        index = part['index']
                        if type(index) is not int or not 0 <= index < 8:
                            raise ValueError('invalid tool index')
                        call = calls.setdefault(index, {'id': '', 'name': '', 'arguments': ''})
                        function = part.get('function', {})
                        for key, value in [('id', part.get('id', '')), ('name', function.get('name', '')),
                                           ('arguments', function.get('arguments', ''))]:
                            if value is not None:
                                call[key] += value
                            if len(call[key]) > (16000 if key == 'arguments' else 128):
                                raise ValueError('tool too large')
                    # reasoning/reasoning_content/thinking fields are never read.
                    text = speech.feed(delta.get('content') or '')
                    content += text
                    text = text[:s.max_response_chars - total]
                    if text:
                        if first and text.strip():
                            first_token()
                            first = False
                        total += len(text)
                        yield text
                        if total >= s.max_response_chars:
                            return  # No tool execution after a truncated response.
                    finish = choice.get('finish_reason')
            speech.feed('', final=True)
            if not calls:
                if finish not in ('stop', 'length'):
                    raise error('ответ оборван до завершения потока.')
                if first:
                    raise error('модель не вернула текст для озвучки; проверьте режим reasoning и лимит токенов.')
                return
            if finish != 'tool_calls' or round_index == 4:
                raise error('незавершённый вызов или превышен лимит циклов инструментов.')
            validated = []
            ids = set()
            for call in calls.values():
                if call['name'] not in self.tools or not call['id'] or call['id'] in ids:
                    raise error('неизвестный инструмент или неверный ID вызова.')
                ids.add(call['id'])
                tool = self.tools[call['name']]
                try:
                    def unique(pairs):
                        result = {}
                        for key, value in pairs:
                            if key in result:
                                raise ValueError('duplicate key')
                            result[key] = value
                        return result
                    args = json.loads(call['arguments'], object_pairs_hook=unique,
                                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    validate_arguments(args, tool.parameters)
                except (ValueError, TypeError, RecursionError, OverflowError):
                    raise error('некорректные аргументы инструмента. Действия не выполнены.') from None
                validated.append((call, tool, args))
            history.append({'role': 'assistant', 'content': content or None, 'tool_calls': [
                {'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': c['arguments']}}
                for c, _, _ in validated]})
            for call, tool, args in validated:
                self.check_cancelled()
                await asyncio.sleep(0)  # Deliver pending Stop before invoking the next tool.
                self.check_cancelled()
                try:
                    async with asyncio.timeout(s.timeout_s):
                        result = await tool.execute(args)
                    self.check_cancelled()
                    result_text = json.dumps(result, ensure_ascii=False, allow_nan=False)
                    if len(result_text) > 16000:
                        raise ValueError('tool result too large')
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise error('не удалось выполнить инструмент. Проверьте доступ и повторите запрос.') from None
                history.append({'role': 'tool', 'tool_call_id': call['id'], 'content': result_text})
            if getattr(self, '_probe', False):
                return

    @staticmethod
    def check_cancelled():
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError
