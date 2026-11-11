import asyncio
from threading import Event
import unittest
from alice_voice.speech import clean_speech, PCM, MAX_COMPRESSED_BYTES, collect_edge_audio
from alice_voice.providers import LLMProvider, ProviderError
from alice_voice.config import Settings
from unittest.mock import patch
import types
import sys


class ServicesTests(unittest.IsolatedAsyncioTestCase):
    def test_decode_enforces_pcm_limit_before_full_allocation(self):
        from alice_voice.speech import decode_edge
        import array
        class Stream:
            closed = False
            consumed = 0
            def __iter__(self): return self
            def __next__(self):
                self.consumed += 1
                if self.consumed > 10: raise StopIteration
                return array.array('h', [0] * 1024)
            def close(self): self.closed = True
        stream = Stream()
        module = types.SimpleNamespace(
            SampleFormat=types.SimpleNamespace(SIGNED16=1),
            stream_memory=lambda *a, **kw: stream,
            decode=lambda *a, **kw: types.SimpleNamespace(samples=array.array('h', [0] * 10240)),
        )
        with patch.dict(sys.modules, {'miniaudio': module}), patch('alice_voice.speech.MAX_PCM_BYTES', 4096):
            with self.assertRaises(RuntimeError):
                decode_edge(b'compressed')
        self.assertTrue(stream.closed)
        self.assertEqual(stream.consumed, 3)

    def test_cleanup_preserves_numbers_and_punctuation(self):
        self.assertEqual(clean_speech('**Цена** — 1 234,50 руб. [Источник](https://example.com)'),
                         'Цена — 1 234,50 руб. Источник')
        self.assertEqual(clean_speech('```python\nprint(42)\n```'), 'print(42)')

    def test_pcm_validation(self):
        with self.assertRaises(ValueError):
            PCM(b'odd', 16000)
        self.assertEqual(PCM(bytes(1024), 16000).channels, 1)

    async def test_edge_buffer_limit(self):
        async def stream():
            yield {'type': 'audio', 'data': bytes(MAX_COMPRESSED_BYTES + 1)}
        with self.assertRaises(RuntimeError):
            await collect_edge_audio(stream(), Event())

    async def test_cancel_edge(self):
        stop = Event()
        stop.set()
        async def stream():
            yield {'type': 'audio', 'data': b'x'}
        with self.assertRaises(asyncio.CancelledError):
            await collect_edge_audio(stream(), stop)

    async def test_cancel_before_server_response(self):
        class Client:
            def stream(self, *args, **kwargs):
                return self
            async def __aenter__(self):
                await asyncio.sleep(30)
            async def __aexit__(self, *args):
                pass
        provider = LLMProvider(Settings(), client=Client())
        task = asyncio.create_task(provider.generate([], lambda: None))
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_error_never_exposes_secret(self):
        class Client:
            def stream(self, *args, **kwargs):
                raise RuntimeError('secret-key server-response')
        provider = LLMProvider(Settings(), client=Client())
        with self.assertRaises(ProviderError) as error:
            await provider.generate([], lambda: None)
        self.assertNotIn('secret', str(error.exception))


if __name__ == '__main__':
    unittest.main()
