import asyncio
import json
import unittest
from contextlib import aclosing
import httpx
from alice_voice.config import Settings
from alice_voice.providers import LLMProvider, ProviderError
from alice_voice.lmstudio import LMStudioProvider, Tool
from alice_voice.secrets import MissingCredentialError, CredentialError


def no_token():
    raise MissingCredentialError('missing')


def sse(*deltas, finish='stop'):
    events = [{'choices': [{'index': 0, 'delta': d}]} for d in deltas]
    events += [{'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]}]
    return ''.join('data: ' + json.dumps(e, ensure_ascii=False) + '\n\n' for e in events).encode() + b'data: [DONE]\n\n'


class LMStudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_text_check_does_not_require_tool_calling(self):
        bodies = []
        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, content=sse({'content': 'Здравствуйте!'}))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            p = LMStudioProvider(self.settings(), client, token_loader=no_token)
            self.assertEqual(await p.probe_text(), 'Здравствуйте!')
        self.assertEqual(bodies[0]['model'], 'actual/model-id')
        self.assertTrue(bodies[0]['stream'])
        self.assertNotIn('tools', bodies[0])

    def settings(self, **kw):
        return Settings(provider='lmstudio', lmstudio_model='actual/model-id', **kw).validate()

    async def test_qwen3_soft_switch_and_other_models_are_not_reconfigured(self):
        for model in ['qwen/qwen3-1.7b', 'qwen/qwen3-8b-gguf', 'other/model', 'qwen3-thinking-2507']:
            bodies = []
            def handler(r):
                bodies.append(json.loads(r.content))
                return httpx.Response(200, content=sse({'content': 'Ответ.'}))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                settings = Settings(provider='lmstudio', lmstudio_model=model)
                p = LMStudioProvider(settings, c, token_loader=no_token)
                _ = [x async for x in p.stream([{'role': 'user', 'content': 'Привет'}], lambda: None)]
            self.assertEqual(bodies[0]['messages'][-1]['content'].endswith('/no_think'), model.startswith('qwen/qwen3-'))

    async def test_server_unavailable_and_timeout_are_distinct(self):
        for exc, expected in [(httpx.ConnectError('private-detail'), 'сервер недоступен'),
                              (httpx.ReadTimeout('private-detail'), 'таймаут')]:
            def handler(r):
                raise exc
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                p = LMStudioProvider(self.settings(), c, token_loader=no_token)
                with self.assertRaisesRegex(ProviderError, expected) as caught:
                    await p.models()
                self.assertNotIn('private-detail', str(caught.exception))

    async def test_capability_probe_validates_selected_model_without_external_tool(self):
        bodies = []
        def handler(r):
            body = json.loads(r.content)
            bodies.append(body)
            return httpx.Response(200, content=sse({'tool_calls': [{'index': 0, 'id': 'probe',
                'function': {'name': 'alice_capability_check', 'arguments': '{"value":"alice-check"}'}}]}, finish='tool_calls'))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            p = LMStudioProvider(self.settings(), c, token_loader=no_token)
            self.assertTrue(await p.probe_tools())
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0]['model'], 'actual/model-id')
        self.assertEqual([t['function']['name'] for t in bodies[0]['tools']], ['alice_capability_check'])

    async def test_selected_model_missing_warmup_never_changes_selection(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:
                httpx.Response(200, json={'data': [{'id': 'another/model'}]}))) as c:
            p = LMStudioProvider(self.settings(), c, token_loader=no_token)
            with self.assertRaisesRegex(ProviderError, 'недоступна'):
                await p.warmup()
            self.assertEqual(p.settings.lmstudio_model, 'actual/model-id')

    async def test_early_close_releases_response_without_waiting_for_full_answer(self):
        closed = asyncio.Event()
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"First."}}]}\n\n'
                await asyncio.Event().wait()
            async def aclose(self):
                closed.set()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Body()))) as c:
            p = LLMProvider(self.settings(), c, lmstudio_token_loader=no_token)
            async with aclosing(p.stream([], lambda: None)) as stream:
                self.assertEqual(await asyncio.wait_for(anext(stream), 1), 'First.')
            self.assertTrue(closed.is_set())

    async def test_cancel_tool_skips_followup_and_next_tool(self):
        entered, closed, later = asyncio.Event(), asyncio.Event(), []
        async def slow(args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        async def next_tool(args):
            later.append(args)
        schema = {'type': 'object', 'properties': {}, 'additionalProperties': False}
        tools = [Tool('slow', '', schema, slow), Tool('next', '', schema, next_tool)]
        requests = []
        def handler(r):
            requests.append(r)
            return httpx.Response(200, content=sse({'tool_calls': [
                {'index': 0, 'id': 'a', 'function': {'name': 'slow', 'arguments': '{}'}},
                {'index': 1, 'id': 'b', 'function': {'name': 'next', 'arguments': '{}'}}]}, finish='tool_calls'))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            p = LMStudioProvider(self.settings(), c, token_loader=no_token, tools=tools)
            async def consume():
                _ = [x async for x in p.stream([], lambda: None)]
            task = asyncio.create_task(consume())
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(closed.is_set())
        self.assertEqual(later, [])
        self.assertEqual(len(requests), 1)

    async def test_tool_cannot_resume_answer_after_swallowing_cancellation(self):
        entered, requests = asyncio.Event(), []
        async def swallowing_tool(args):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return {'done': True}
        tool = Tool('existing', '', {'type': 'object', 'properties': {}}, swallowing_tool)
        def handler(r):
            requests.append(r)
            if len(requests) > 1:
                return httpx.Response(200, content=sse({'content': 'must not resume'}))
            return httpx.Response(200, content=sse({'tool_calls': [{'index': 0, 'id': 'a',
                'function': {'name': 'existing', 'arguments': '{}'}}]}, finish='tool_calls'))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            p = LMStudioProvider(self.settings(), c, token_loader=no_token, tools=[tool])
            async def consume():
                _ = [x async for x in p.stream([], lambda: None)]
            task = asyncio.create_task(consume())
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        self.assertEqual(len(requests), 1)

    async def test_nested_reasoning_never_reaches_speech(self):
        raw = '<think>outer<think>inner</think>still hidden</think>Ответ.'
        for split in range(1, len(raw)):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
                    200, content=sse({'content': raw[:split]}, {'content': raw[split:]})))) as c:
                p = LMStudioProvider(self.settings(), c, token_loader=no_token)
                self.assertEqual(''.join([x async for x in p.stream([], lambda: None)]), 'Ответ.')

    async def test_eof_without_finish_and_reasoning_only_are_errors(self):
        for body in [b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
                     sse({'reasoning_content': 'not speech'}), sse({'content': '   '})]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))) as c:
                p = LMStudioProvider(self.settings(), c, token_loader=no_token)
                with self.assertRaises(ProviderError):
                    _ = [x async for x in p.stream([], lambda: None)]

    async def test_models_real_ids_pool_auth_and_reasoning_filter(self):
        seen, first = [], []
        def handler(r):
            seen.append(r)
            if r.method == 'GET':
                return httpx.Response(200, json={'data': [{'id': 'actual/model-id'}]})
            return httpx.Response(200, content=sse({'reasoning_content': 'secret'},
                {'content': '<thi'}, {'content': 'nk>hidden</th'}, {'content': 'ink>Привет'}, {'content': '!'}))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            p = LMStudioProvider(self.settings(), c, token_loader=lambda: 'test-only')
            self.assertEqual(await p.models(), ['actual/model-id'])
            self.assertEqual(''.join([x async for x in p.stream([], lambda: first.append(1))]), 'Привет!')
            self.assertEqual(len(first), 1)
            self.assertIs(p.client, c)
        self.assertEqual(seen[0].url.path, '/v1/models')
        self.assertTrue(all(r.headers['authorization'] == 'Bearer test-only' for r in seen))
        body = json.loads(seen[-1].content)
        self.assertEqual(body['model'], 'actual/model-id')
        self.assertTrue(body['stream'])

    async def test_fragmented_tool_arguments_validated_and_followup(self):
        executed, requests = [], []
        async def handler_tool(args):
            executed.append(args)
            return {'ok': True}
        tool = Tool('existing', 'Test fixture only', {'type': 'object', 'properties': {
            'text': {'type': 'string'}}, 'required': ['text'], 'additionalProperties': False}, handler_tool)
        def handler(r):
            requests.append(json.loads(r.content))
            if len(requests) == 1:
                return httpx.Response(200, content=sse({'tool_calls': [{'index': 0, 'id': 'call-1',
                    'type': 'function', 'function': {'name': 'existing', 'arguments': '{"te'}}]},
                    {'tool_calls': [{'index': 0, 'function': {'arguments': 'xt":"начало"}'}}]}, finish='tool_calls'))
            return httpx.Response(200, content=sse({'content': 'Готово.'}))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            p = LMStudioProvider(self.settings(), c, token_loader=no_token, tools=[tool])
            self.assertEqual(''.join([x async for x in p.stream([], lambda: None)]), 'Готово.')
        self.assertEqual(executed, [{'text': 'начало'}])
        self.assertEqual(requests[1]['messages'][-1]['role'], 'tool')
        self.assertEqual(requests[1]['messages'][-1]['tool_call_id'], 'call-1')

    async def test_invalid_unknown_truncated_tool_never_runs(self):
        for name, args, finish in [('existing', '{bad', 'tool_calls'), ('unknown', '{}', 'tool_calls'),
                ('existing', '{"text":4}', 'tool_calls'), ('existing', '{"text":"a","extra":1}', 'tool_calls'),
                ('existing', '{"text":"a"}', 'length')]:
            executed = []
            async def execute(a):
                executed.append(a)
            t = Tool('existing', '', {'type': 'object', 'properties': {'text': {'type': 'string'}},
                                     'required': ['text'], 'additionalProperties': False}, execute)
            def handler(r):
                return httpx.Response(200, content=sse({'tool_calls': [{'index': 0, 'id': 'x',
                    'function': {'name': name, 'arguments': args}}]}, finish=finish))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                p = LMStudioProvider(self.settings(), c, token_loader=no_token, tools=[t])
                with self.assertRaises(ProviderError):
                    _ = [x async for x in p.stream([], lambda: None)]
            self.assertEqual(executed, [])

    async def test_http_errors_are_safe_and_no_fallback(self):
        for status, message in [(401, 'авторизац'), (403, 'авторизац'), (404, 'модель'), (503, 'сервер')]:
            calls = []
            def handler(r):
                calls.append(r)
                return httpx.Response(status, text='private-server-body token-secret')
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                p = LMStudioProvider(self.settings(), c, token_loader=no_token)
                with self.assertRaises(ProviderError) as caught:
                    _ = [x async for x in p.stream([], lambda: None)]
                self.assertIn(message, str(caught.exception).lower())
                self.assertNotIn('private', str(caught.exception))
                self.assertEqual(len(calls), 1)

    async def test_credential_error_does_not_send_anonymous_request(self):
        def fail():
            raise CredentialError('private-native-error')
        def handler(r):
            self.fail('must not send')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            p = LMStudioProvider(self.settings(), c, token_loader=fail)
            with self.assertRaisesRegex(ProviderError, 'Credential Manager'):
                await p.models()

    async def test_cancel_closes_http_and_no_late_text(self):
        entered, closed = asyncio.Event(), asyncio.Event()
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield sse({'content': 'Начало.'}).split(b'data: [DONE]')[0].split(b'data: {"choices": [{"index": 0, "delta": {},')[0]
                entered.set()
                await asyncio.Event().wait()
                yield sse({'content': 'Поздний текст'})
            async def aclose(self):
                closed.set()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Body()))) as c:
            p = LMStudioProvider(self.settings(), c, token_loader=no_token)
            received = []
            async def consume():
                async with aclosing(p.stream([], lambda: None)) as stream:
                    async for text in stream:
                        received.append(text)
            task = asyncio.create_task(consume())
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel(); task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(closed.is_set())
            self.assertEqual(received, ['Начало.'])

    async def test_facade_uses_lmstudio_for_warmup_and_generation(self):
        paths = []
        def handler(r):
            paths.append(r.url.path)
            return (httpx.Response(200, json={'data': [{'id': 'actual/model-id'}]}) if r.method == 'GET'
                    else httpx.Response(200, content=sse({'content': 'Ответ.'})))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            p = LLMProvider(self.settings(), c, lmstudio_token_loader=no_token)
            await p.warmup()
            self.assertEqual(await p.generate([], lambda: None), 'Ответ.')
        self.assertIn('/v1/models', paths)
        self.assertIn('/v1/chat/completions', paths)
        self.assertFalse(any('/api/' in path for path in paths))
