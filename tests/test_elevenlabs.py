"""ElevenLabs config and HTTP contracts; no API calls or paid synthesis."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

import httpx
from PyQt6.QtCore import QCoreApplication

from alice_voice.config import Settings
from alice_voice.metrics import LatencyMetrics
from alice_voice import providers
from alice_voice.speech import PCM
from alice_voice.workers import DialogRuntime, WorkerSignals


APP = QCoreApplication.instance() or QCoreApplication([])


def configured(**changes):
    return Settings(synthesizer='elevenlabs',
                    elevenlabs_voice_id='testVoice123', **changes)


class ElevenLabsConfigTests(unittest.TestCase):
    def test_runtime_selects_elevenlabs_and_closes_http_client_on_shutdown(self):
        runtime = DialogRuntime(configured(), WorkerSignals(), LatencyMetrics())
        runtime.start()
        try:
            self.assertTrue(runtime.ready.wait(3))
            self.assertIsInstance(runtime.synth, providers.ElevenLabsTTSProvider)
            self.assertFalse(runtime.synth.client.is_closed)
        finally:
            runtime.shutdown()
            runtime.join(5)
        self.assertFalse(runtime.is_alive())
        self.assertTrue(runtime.synth.client.is_closed)

    def test_new_fields_roundtrip_and_old_configs_keep_defaults(self):
        settings = configured(elevenlabs_model_id='eleven_multilingual_v2')
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            settings.save(path)
            self.assertEqual(Settings.load(path), settings)
        self.assertNotIn('elevenlabs_api_key', Settings.from_dict({'synthesizer': 'edge'}).to_dict())
        self.assertNotIn('fake-eleven-key', repr(settings))

    def test_invalid_ids_and_header_values_are_rejected_without_echo(self):
        for changes in ({'elevenlabs_voice_id': '../private-value'},
                        {'elevenlabs_model_id': ''}):
            with self.subTest(field=next(iter(changes))):
                with self.assertRaises(ValueError) as error:
                    replace(configured(), **changes).validate()
                self.assertNotIn('private-value', str(error.exception))


class ElevenLabsProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch('alice_voice.providers.load_elevenlabs_key', return_value='fake-eleven-key')
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_pcm_request_and_sentence_pipeline_preserve_order(self):
        requests, emitted, errors = [], [], []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=b'\x01\x00' * 64,
                                  headers={'content-type': 'audio/pcm'})
        class LLM:
            async def stream(self, messages, first):
                yield 'Первое предложение.'
                yield ' Второе предложение.'
        settings = configured()
        signals = WorkerSignals()
        runtime = DialogRuntime(settings, signals, LatencyMetrics())
        runtime.provider = LLM()
        def ready(token, pcm, when, sequence):
            emitted.append((sequence, pcm))
            runtime._release_pcm(token, sequence)
        signals.pcm.connect(ready)
        signals.failed.connect(lambda token, message: errors.append(message))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime.synth = providers.ElevenLabsTTSProvider(settings, client=client)
            await asyncio.wait_for(runtime._process(7, 'Сколько будет два плюс два?', Event()), 2)
        self.assertEqual(errors, [])
        self.assertEqual([json.loads(r.content)['text'] for r in requests],
                         ['Первое предложение.', 'Второе предложение.'])
        for request in requests:
            self.assertEqual(str(request.url),
                             'https://api.elevenlabs.io/v1/text-to-speech/testVoice123/stream?output_format=pcm_24000')
            self.assertEqual(request.headers['xi-api-key'], 'fake-eleven-key')
            self.assertEqual(json.loads(request.content)['model_id'], 'eleven_multilingual_v2')
        self.assertEqual([sequence for sequence, pcm in emitted], [0, 1])
        self.assertTrue(all(pcm.data == b'\x01\x00' * 64 and pcm.sample_rate == 24000
                            and pcm.channels == 1 for sequence, pcm in emitted))
        self.assertEqual(len(runtime.history), 2)

    async def test_missing_key_or_voice_fails_before_http(self):
        requests = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: requests.append(r))) as client:
            for settings, key in ((configured(), ''),
                                  (replace(configured(), elevenlabs_voice_id=''), 'fake-eleven-key')):
                with self.assertRaises(providers.ProviderError):
                    await providers.ElevenLabsTTSProvider(settings, client=client,
                                                         key_loader=lambda: key).synthesize('Привет', Event())
        self.assertEqual(requests, [])

    async def test_http_errors_and_bad_audio_are_safe(self):
        responses = [(401, b'fake-eleven-key', 'application/json'),
                     (429, b'fake-eleven-key', 'application/json'),
                     (200, b'', 'audio/pcm'), (200, b'odd', 'audio/pcm'),
                     (200, b'{"error": "fake-eleven-key"}', 'application/json')]
        for status, content, kind in responses:
            with self.subTest(status=status, kind=kind, size=len(content)):
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda r: httpx.Response(status, content=content, headers={'content-type': kind}))) as client:
                    with self.assertRaises(providers.ProviderError) as error:
                        await providers.ElevenLabsTTSProvider(configured(), client=client).synthesize('Привет', Event())
                    self.assertNotIn('fake-eleven-key', str(error.exception))

    async def test_pcm_buffer_is_bounded_and_response_closed(self):
        class Audio(httpx.AsyncByteStream):
            closed = False
            async def __aiter__(self):
                yield b'\x00' * 65536
                yield b'\x00' * 65536
            async def aclose(self):
                self.closed = True
        audio = Audio()
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(200, stream=audio))) as client:
            with patch('alice_voice.providers.MAX_PCM_BYTES', 65536):
                with self.assertRaises(providers.ProviderError):
                    await providers.ElevenLabsTTSProvider(configured(), client=client).synthesize('Привет', Event())
        self.assertTrue(audio.closed)

    async def test_cancelled_http_closes_response(self):
        entered = asyncio.Event()
        class Audio(httpx.AsyncByteStream):
            closed = False
            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b'\x00\x00'
            async def aclose(self):
                self.closed = True
        audio = Audio()
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(200, stream=audio))) as client:
            tts = providers.ElevenLabsTTSProvider(configured(), client=client)
            task = asyncio.create_task(tts.synthesize('Привет', Event()))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(audio.closed)

    async def test_timeout_is_safe_and_closes_response(self):
        class Audio(httpx.AsyncByteStream):
            closed = False
            async def __aiter__(self):
                await asyncio.Event().wait()
                yield b'\x00\x00'
            async def aclose(self):
                self.closed = True
        audio = Audio()
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(200, stream=audio))) as client:
            tts = providers.ElevenLabsTTSProvider(configured(), client=client)
            tts.timeout = .03
            with self.assertRaisesRegex(providers.ProviderError, 'таймаут'):
                await tts.synthesize('Привет', Event())
        self.assertTrue(audio.closed)

    async def test_mp3_is_decoded_in_executor_to_pcm(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=b'mp3-fixture', headers={'content-type': 'audio/mpeg'})
        def decode(data, cancel):
            self.assertEqual(data, b'mp3-fixture')
            self.assertFalse(cancel.is_set())
            return PCM(b'\x01\x00' * 64, 24000)
        with ThreadPoolExecutor(max_workers=1) as executor:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch('alice_voice.providers.decode_edge', side_effect=decode):
                    tts = providers.ElevenLabsTTSProvider(configured(), executor=executor,
                                                       client=client, output_format='mp3_44100_128')
                    pcm = await tts.synthesize('Привет', Event())
        self.assertEqual(pcm.data, b'\x01\x00' * 64)
        self.assertEqual(requests[0].url.params['output_format'], 'mp3_44100_128')

    async def test_close_only_closes_owned_client(self):
        tts = providers.ElevenLabsTTSProvider(configured())
        await tts.close()
        self.assertTrue(tts.client.is_closed)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: None)) as client:
            tts = providers.ElevenLabsTTSProvider(configured(), client=client)
            await tts.close()
            self.assertFalse(client.is_closed)
