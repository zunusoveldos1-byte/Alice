import asyncio
import base64
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from time import perf_counter
import json
import httpx
from typing import Protocol

from .secrets import load_gemini_key, load_elevenlabs_key, CredentialError, MissingCredentialError
from .speech import PCM, MAX_PCM_BYTES, MAX_COMPRESSED_BYTES, decode_edge
from .utils.vision import MAX_IMAGE_BYTES


class ProviderError(RuntimeError):
    pass


async def gemini_quota_error(response):
    """Read only bounded structured quota metadata; never echo the error body."""
    daily = False
    data = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=4096):
        if len(data) + len(chunk) > 32768:
            break
        data.extend(chunk)
    try:
        body = json.loads(data)
        error = body.get('error') if isinstance(body, dict) else None
        details = error.get('details') if isinstance(error, dict) else None
        for detail in details if isinstance(details, list) else []:
            violations = detail.get('violations') if isinstance(detail, dict) else None
            for item in violations if isinstance(violations, list) else []:
                identity = item.get('quotaId') if isinstance(item, dict) else None
                if isinstance(identity, str) and 'PerDay' in identity:
                    daily = True
    except (ValueError, TypeError):
        pass
    if daily:
        return ('Gemini: исчерпана суточная квота выбранной модели (HTTP 429). '
                'Дождитесь восстановления квоты или выберите другую доступную модель. '
                'Проверьте лимиты проекта в Google AI Studio. Локальная озвучка Silero не заменяет LLM.')
    return ('Gemini: превышен лимит запросов или временно недоступны ресурсы (HTTP 429). '
            'Повторите позже и проверьте квоту проекта в Google AI Studio.')


class ElevenLabsTTSProvider:
    accepts_fragments = True
    """Synthesize one queued sentence to memory-only PCM using ElevenLabs.

    The dialog runtime serializes calls and owns cancellation/backpressure.
    MP3 responses are decoded on its synthesis executor, outside the GUI loop.
    """
    def __init__(self, settings, executor=None, client=None, output_format='pcm_24000', key_loader=None):
        settings.validate()
        if output_format not in ('pcm_24000', 'mp3_44100_128'):
            raise ValueError('Неподдерживаемый формат ElevenLabs.')
        self.settings, self.executor = settings, executor
        self.key_loader = key_loader or load_elevenlabs_key
        self.timeout = settings.timeout_s
        self.output_format = output_format
        self.owns_client = client is None
        if client is None:
            import httpx
            client = httpx.AsyncClient(timeout=settings.timeout_s, trust_env=False,
                                       limits=httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=60))
        self.client = client

    async def close(self):
        if self.owns_client:
            await self.client.aclose()

    def check_configuration(self):
        try:
            key = self.key_loader().strip()
        except MissingCredentialError:
            raise ProviderError('Ключ ElevenLabs отсутствует. Добавьте ключ в настройках голоса.') from None
        except Exception:
            raise ProviderError('Не удалось прочитать ключ ElevenLabs из Windows Credential Manager. '
                                'Проверьте доступ к хранилищу Windows.') from None
        if not key:
            raise ProviderError('Ключ ElevenLabs отсутствует. Добавьте ключ в настройках голоса.')
        if any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ProviderError('Неверный формат сохранённого ключа ElevenLabs. Замените ключ в настройках голоса.')
        if not self.settings.elevenlabs_voice_id:
            raise ProviderError('Укажите Voice ID ElevenLabs в настройках голоса.')
        return key

    async def synthesize(self, text, cancel) -> PCM:
        if cancel.is_set():
            raise asyncio.CancelledError
        key = self.check_configuration()
        if not text.strip():
            raise ProviderError('Нет текста для озвучки ElevenLabs.')
        url = f'https://api.elevenlabs.io/v1/text-to-speech/{self.settings.elevenlabs_voice_id}'
        mp3 = self.output_format.startswith('mp3_')
        limit = MAX_COMPRESSED_BYTES if mp3 else MAX_PCM_BYTES
        try:
            async with asyncio.timeout(self.timeout):
                async with self.client.stream(
                        'POST', url, params={'output_format': self.output_format},
                        headers={'xi-api-key': key, 'Accept': 'audio/mpeg' if mp3 else 'audio/pcm'},
                        json={'text': text, 'model_id': self.settings.elevenlabs_model_id}) as response:
                    if response.status_code != 200:
                        raise ProviderError(f'ElevenLabs отклонил запрос (HTTP {response.status_code}). '
                                            'Проверьте ключ, голос и лимиты.')
                    content_type = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
                    allowed = ('audio/mpeg', 'application/octet-stream') if mp3 else (
                        'audio/pcm', 'audio/raw', 'audio/x-pcm', 'application/octet-stream')
                    if content_type and content_type not in allowed:
                        raise ProviderError('ElevenLabs вернул неожиданный формат аудио.')
                    data = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if cancel.is_set():
                            raise asyncio.CancelledError
                        if len(data) + len(chunk) > limit:
                            raise ProviderError('Ответ ElevenLabs превысил ограничение аудиобуфера.')
                        data.extend(chunk)
                if cancel.is_set():
                    raise asyncio.CancelledError
                if not data or (not mp3 and len(data) % 2):
                    raise ProviderError('ElevenLabs вернул пустое или повреждённое аудио.')
                if mp3:
                    pcm = await asyncio.get_running_loop().run_in_executor(
                        self.executor, decode_edge, bytes(data), cancel)
                else:
                    pcm = PCM(bytes(data), 24000)
                if cancel.is_set():
                    raise asyncio.CancelledError
                return pcm
        except asyncio.CancelledError:
            raise
        except ProviderError:
            raise
        except TimeoutError:
            raise ProviderError('Истёк таймаут синтеза ElevenLabs.') from None
        except Exception:
            raise ProviderError('Не удалось получить или декодировать аудио ElevenLabs. '
                                'Проверьте подключение и настройки голоса.') from None

    async def stream_pcm(self, text, cancel):
        """Yield aligned PCM while HTTP is still arriving; pause excludes timeout."""
        if self.output_format.startswith('mp3_'):
            yield await self.synthesize(text, cancel)
            return
        remaining = self.timeout
        try:
            async with aclosing(self._stream_pcm(text, cancel)) as stream:
                while True:
                    if cancel.is_set():
                        raise asyncio.CancelledError
                    started = perf_counter()
                    try:
                        async with asyncio.timeout(remaining):
                            pcm = await anext(stream)
                    except StopAsyncIteration:
                        return
                    finally:
                        remaining -= perf_counter() - started
                    yield pcm
        except (asyncio.CancelledError, ProviderError):
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderError('Истёк таймаут синтеза ElevenLabs.') from None
        except Exception:
            raise ProviderError('Не удалось получить поток PCM ElevenLabs. Проверьте подключение и настройки голоса.') from None

    async def _stream_pcm(self, text, cancel):
        if cancel.is_set():
            raise asyncio.CancelledError
        key = self.check_configuration()
        if not text.strip():
            raise ProviderError('Нет текста для озвучки ElevenLabs.')
        url = f'https://api.elevenlabs.io/v1/text-to-speech/{self.settings.elevenlabs_voice_id}/stream'
        total, pending = 0, bytearray()
        async with self.client.stream('POST', url, params={'output_format': self.output_format},
                                      headers={'xi-api-key': key, 'Accept': 'audio/pcm'},
                                      json={'text': text, 'model_id': self.settings.elevenlabs_model_id}) as response:
            if response.status_code != 200:
                raise ProviderError(f'ElevenLabs отклонил запрос (HTTP {response.status_code}). Проверьте ключ, голос и лимиты.')
            kind = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
            if kind and kind not in ('audio/pcm', 'audio/raw', 'audio/x-pcm', 'application/octet-stream'):
                raise ProviderError('ElevenLabs вернул неожиданный формат аудио.')
            # No HTTPX chunk_size: it would wait for a full aggregate block.
            async for data in response.aiter_bytes():
                if cancel.is_set():
                    raise asyncio.CancelledError
                total += len(data)
                if total > MAX_PCM_BYTES:
                    raise ProviderError('Ответ ElevenLabs превысил ограничение аудиобуфера.')
                pending.extend(data)
                while len(pending) >= 960:  # 20 ms minimum; <=100 ms per packet.
                    count = min(4800, len(pending) // 2 * 2)
                    packet = bytes(pending[:count])
                    del pending[:count]
                    if cancel.is_set():
                        raise asyncio.CancelledError
                    yield PCM(packet, 24000)
            if not total or len(pending) % 2:
                raise ProviderError('ElevenLabs вернул пустое или повреждённое аудио.')
            if pending:
                if cancel.is_set():
                    raise asyncio.CancelledError
                yield PCM(bytes(pending), 24000)


class Provider(Protocol):
    def stream(self, messages: list[dict], first_token: Callable[[], None],
               image_jpeg: bytes | None = None) -> AsyncIterator[str]: ...
    async def generate(self, messages: list[dict], first_token,
                       image_jpeg: bytes | None = None) -> str: ...


class LLMProvider:
    """Shared streaming interface. Owns one async client per dialog runtime."""
    def __init__(self, settings, client=None, key_loader=load_gemini_key, *, lmstudio_token_loader=None, tools=()):
        self.settings = settings
        self.key_loader = key_loader
        self.owns_client = client is None
        if client is None:
            import httpx
            timeout = (httpx.Timeout(settings.timeout_s, connect=min(5, settings.timeout_s))
                       if settings.provider == 'lmstudio' else settings.timeout_s)
            client = httpx.AsyncClient(timeout=timeout, trust_env=False,
                                       limits=httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=60))
        self.client = client
        from .lmstudio import LMStudioProvider
        self.lmstudio = LMStudioProvider(settings, client, lmstudio_token_loader, tools)
        self.checked_model = False
        self._model_lock = asyncio.Lock()

    async def close(self):
        if self.owns_client:
            await self.client.aclose()

    async def _gemini_check(self, headers):
        if self.checked_model:
            return
        async with self._model_lock:
            if self.checked_model:
                return
            url = f'https://generativelanguage.googleapis.com/v1beta/models/{self.settings.gemini_model}'
            response = await self.client.get(url, headers=headers)
            if response.status_code == 429:
                raise ProviderError(await gemini_quota_error(response))
            if response.status_code != 200:
                raise ProviderError('Выбранная модель Gemini недоступна для этого ключа. Проверьте имя модели и доступ API.')
            if 'generateContent' not in response.json().get('supportedGenerationMethods', []):
                raise ProviderError('Выбранная модель Gemini не поддерживает генерацию текста.')
            self.checked_model = True

    async def warmup(self):
        """Use this runtime's existing client; never choose another model."""
        if self.settings.provider == 'lmstudio':
            await self.lmstudio.warmup()
        elif self.settings.provider == 'gemini':
            # Metadata/TLS warmup, no synthetic conversation or paid generation.
            await self._gemini_check({'x-goog-api-key': self.key_loader()})
        else:
            response = await self.client.post(self.settings.ollama_url.rstrip('/') + '/api/generate',
                                              json={'model': self.settings.ollama_model, 'prompt': '',
                                                    'stream': False, 'keep_alive': '5m'})
            if response.status_code == 404:
                raise ProviderError('Выбранная модель Ollama не найдена. Выберите установленную модель в настройках.')
            if response.status_code != 200:
                raise ProviderError('Не удалось прогреть выбранную модель Ollama.')

    async def generate(self, messages: list[dict], first_token: Callable[[], None],
                       image_jpeg: bytes | None = None) -> str:
        chunks = [delta async for delta in self.stream(messages, first_token, image_jpeg=image_jpeg)]
        raw_answer = ''.join(chunks)
        answer = raw_answer.strip()
        if len(raw_answer) >= self.settings.max_response_chars:
            boundary = max(answer.rfind('.'), answer.rfind('!'), answer.rfind('?'))
            answer = answer[:boundary + 1] if boundary > 0 else answer.rsplit(' ', 1)[0] + '…'
        return answer

    async def stream(self, messages: list[dict], first_token: Callable[[], None],
                     image_jpeg: bytes | None = None) -> AsyncIterator[str]:
        """Yield bounded text deltas and close HTTP on cancellation/early exit."""
        remaining = self.settings.timeout_s
        try:
            async with aclosing(self._stream(messages, first_token, image_jpeg)) as stream:
                while True:
                    started = perf_counter()
                    try:
                        # No armed timer across yield: downstream playback may be paused.
                        async with asyncio.timeout(remaining):
                            delta = await anext(stream)
                    except StopAsyncIteration:
                        return
                    finally:
                        remaining -= perf_counter() - started
                    yield delta
        except asyncio.CancelledError:
            raise
        except ProviderError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            if self.settings.provider == 'ollama':
                raise ProviderError('Сервер Ollama не ответил: истёк таймаут. Проверьте, запущен ли Ollama.') from None
            raise ProviderError('Истёк таймаут запроса модели.') from None
        except httpx.RequestError:
            if self.settings.provider == 'ollama':
                raise ProviderError('Сервер Ollama недоступен. Запустите Ollama и проверьте URL в настройках.') from None
            raise ProviderError('Не удалось подключиться к серверу модели. Проверьте сеть и настройки провайдера.') from None
        except Exception:
            raise ProviderError('Ошибка связи с моделью или неверный ответ API. Проверьте настройки провайдера.') from None

    async def _stream(self, messages, first_token, image_jpeg=None) -> AsyncIterator[str]:
        s = self.settings
        if image_jpeg is not None:
            if s.provider != 'gemini':
                raise ProviderError('Для анализа экрана выберите Gemini с поддержкой изображений.')
            if (not isinstance(image_jpeg, bytes) or not image_jpeg.startswith(b'\xff\xd8\xff')
                    or len(image_jpeg) > MAX_IMAGE_BYTES or not messages or messages[-1]['role'] != 'user'):
                raise ProviderError('Неверное или слишком большое изображение экрана.')
        if s.provider == 'lmstudio':
            async with aclosing(self.lmstudio.stream(messages, first_token)) as stream:
                async for text in stream:
                    yield text
            return
        headers = {}
        if s.provider == 'ollama':
            url = s.ollama_url.rstrip('/') + '/api/chat'
            body = {'model': s.ollama_model, 'stream': True,
                    'messages': [{'role': 'system', 'content': s.system_prompt}] + messages,
                    'options': {'num_predict': s.max_tokens}}
            if s.ollama_model.split(':', 1)[0].casefold() in ('qwen3', 'qwen3.5'):
                # Qwen3 otherwise spends the short voice-answer budget on
                # hidden reasoning and may return no message.content at all.
                body['think'] = False
        else:
            try:
                headers = {'x-goog-api-key': self.key_loader()}
            except CredentialError as error:
                raise ProviderError(str(error)) from None
            await self._gemini_check(headers)
            url = f'https://generativelanguage.googleapis.com/v1beta/models/{s.gemini_model}:streamGenerateContent?alt=sse'
            body = {'systemInstruction': {'parts': [{'text': s.system_prompt}]},
                    'contents': [{'role': 'model' if m['role'] == 'assistant' else 'user',
                                  'parts': [{'text': m['content']}]} for m in messages],
                    'generationConfig': {'maxOutputTokens': s.max_tokens}}
            if image_jpeg is not None:
                body['contents'][-1]['parts'].append({'inline_data': {
                    'mime_type': 'image/jpeg', 'data': base64.b64encode(image_jpeg).decode('ascii')}})
        count = 0
        first = True
        has_text = False
        async with self.client.stream('POST', url, headers=headers, json=body) as response:
            if response.status_code != 200:
                if s.provider == 'gemini' and response.status_code == 429:
                    raise ProviderError(await gemini_quota_error(response))
                if s.provider == 'ollama' and response.status_code == 404:
                    raise ProviderError('Выбранная модель Ollama не найдена (HTTP 404). '
                                        'Выберите установленную модель в настройках; '
                                        'список можно посмотреть командой ollama list.')
                if s.provider == 'ollama' and response.status_code in (502, 503, 504):
                    raise ProviderError(f'Сервер Ollama недоступен (HTTP {response.status_code}). '
                                        'Проверьте работу сервера и повторите запрос.')
                raise ProviderError(f'Модель отклонила запрос (HTTP {response.status_code}). Проверьте модель, ключ и лимиты.')
            async for line in response.aiter_lines():
                if not line:
                    continue
                if s.provider == 'gemini':
                    if not line.startswith('data:'):
                        continue
                    data = json.loads(line[5:].strip())
                    if 'error' in data:
                        raise ProviderError('Gemini сообщил об ошибке обработки запроса.')
                    candidates = data.get('candidates', [])
                    parts = candidates[0].get('content', {}).get('parts', []) if candidates else []
                    text = ''.join(p.get('text', '') for p in parts if not p.get('thought', False))
                else:
                    data = json.loads(line)
                    if 'error' in data:
                        raise ProviderError('Ollama сообщил об ошибке. Проверьте наличие выбранной модели.')
                    text = data.get('message', {}).get('content', '')
                if not text:
                    continue
                if first:
                    first_token()
                    first = False
                remaining = s.max_response_chars - count
                delta = text[:remaining]
                has_text = has_text or bool(delta.strip())
                count += len(delta)
                yield delta
                if count >= s.max_response_chars:
                    break
        if not has_text:
            raise ProviderError('Модель не вернула текстовый ответ.')
