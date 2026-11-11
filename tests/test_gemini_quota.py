"""Provider quota messages must be useful and never expose response secrets."""
import unittest
import httpx
from alice_voice.config import Settings
from alice_voice.providers import LLMProvider, ProviderError


class GeminiQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def test_qwen3_voice_request_disables_hidden_thinking_but_keeps_other_models_unchanged(self):
        import json
        for name in ('qwen3:1.7b', 'qwen3.5:4b', 'other-model'):
            requests = []
            def handler(request):
                requests.append(json.loads(request.content))
                return httpx.Response(200, content=b'{"message":{"thinking":"hidden"}}\n{"message":{"content":"Hello."}}\n')
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                provider = LLMProvider(Settings(provider='ollama',ollama_model=name),client)
                self.assertEqual(await provider.generate([],lambda:None),'Hello.')
            if name in ('qwen3:1.7b', 'qwen3.5:4b'):
                self.assertIs(requests[0].get('think'), False)
            else:
                self.assertNotIn('think', requests[0])

    async def error(self, body, *, metadata_status=200):
        requests = []
        def handler(request):
            requests.append(request.method)
            if request.method == 'GET' and metadata_status == 200:
                return httpx.Response(200, json={'supportedGenerationMethods':['generateContent']})
            return httpx.Response(429, json=body)
        settings = Settings(provider='gemini', synthesizer='silero')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            p = LLMProvider(settings, client, key_loader=lambda:'private-api-key')
            with self.assertRaises(ProviderError) as caught:
                await p.generate([{'role':'user','content':'Привет'}], lambda:None)
        self.assertEqual(settings.provider, 'gemini')
        self.assertEqual(settings.synthesizer, 'silero')
        self.assertLessEqual(requests.count('POST'), 1)
        self.assertNotIn('private-api-key', str(caught.exception))
        self.assertNotIn('secret-response', str(caught.exception))
        return str(caught.exception)

    async def test_daily_limit_names_cause_and_action_without_retrying(self):
        message = await self.error({'error':{'message':'secret-response private-api-key', 'details':[
            {'@type':'type.googleapis.com/google.rpc.QuotaFailure','violations':[
                {'quotaId':'GenerateRequestsPerDayPerProjectPerModel-FreeTier','quotaValue':'20'}]}]}})
        self.assertIn('суточн', message)
        self.assertIn('429', message)
        self.assertIn('квот', message)

    async def test_minute_limit_is_not_reported_as_daily_exhaustion(self):
        message = await self.error({'error':{'details':[{'violations':[
            {'quotaId':'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'}]}]}})
        self.assertIn('лимит', message)
        self.assertNotIn('суточн', message)

    async def test_malformed_error_and_metadata_quota_have_safe_messages(self):
        for body in ({'error':{'details':'secret-response'}}, ['secret-response'], {'error':None}):
            self.assertIn('429', await self.error(body))
        self.assertIn('429', await self.error({'error':{}}, metadata_status=429))
