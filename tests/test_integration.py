"""Offline integration tests: no live microphone, keys or external services."""
import asyncio
import inspect
import json
from pathlib import Path
from threading import Event
from time import perf_counter, sleep
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QCoreApplication
from alice_voice.config import Settings
from alice_voice.controller import VoiceSessionController
from alice_voice.metrics import LatencyMetrics
from alice_voice.playback import EchoGuard, PCMPlayback
from alice_voice.providers import LLMProvider, ProviderError
from alice_voice.speech import PCM
from alice_voice.workers import CaptureWorker, DialogRuntime, WorkerSignals

APP = QCoreApplication.instance() or QCoreApplication([])


def pump_until(predicate, timeout=3):
    deadline = perf_counter() + timeout
    while not predicate() and perf_counter() < deadline:
        APP.processEvents()
        sleep(.005)
    APP.processEvents()
    return predicate()


class IntegrationTests(unittest.TestCase):
    def test_settings_save_once_requests_async_runtime_shutdown(self):
        class Runtime:
            stopped = False
            def shutdown(self): self.stopped = True
        controller = VoiceSessionController(Settings())
        old_runtime = Runtime()
        controller.runtime = old_runtime
        updated = Settings(provider='gemini')
        controller.apply_settings(updated)
        self.assertTrue(old_runtime.stopped)
        self.assertIs(controller.settings, updated)
        controller.runtime = None
        controller.shutdown()
        controller._poll()

    def test_stop_rejects_already_queued_phrase(self):
        class Runtime:
            submitted = False
            def cancel(self): pass
            def submit(self, token, text):
                self.submitted = True
                return True
        controller = VoiceSessionController(Settings())
        controller.mic_state = 'listening'
        controller.runtime = Runtime()
        earlier = perf_counter() - 1
        controller.cancel_response()
        controller._phrase(0, {'id': 'stale', 'text': 'привет', 'start': earlier,
                               'speech_end': earlier + .1, 'ready': earlier + .2})
        self.assertFalse(controller.runtime.submitted)
        controller.runtime = None
        controller.shutdown()
        controller._poll()

    def test_overflow_during_finalization_discards_phrase(self):
        signals = WorkerSignals()
        phrases, errors = [], []
        signals.phrase.connect(lambda epoch, phrase: phrases.append(phrase))
        signals.error.connect(lambda epoch, error: errors.append(error))
        shared = {}
        class Capture:
            def __init__(self, inbox, device):
                shared['inbox'] = inbox
                self.inbox = inbox
            def open(self):
                for i in range(3):
                    self.inbox.push(bytes([1 if i == 0 else 0]) * 1024, perf_counter())
            def close(self): pass
        class VAD:
            def __init__(self, path): pass
            def __call__(self, pcm, sample_rate):
                assert sample_rate == 16000
                return .9 if pcm[0] else 0
            def reset(self): pass
        class STT:
            def __init__(self, path): pass
            def reset(self): pass
            def feed(self, pcm): return 'привет'
            def finalize(self):
                shared['inbox'].damaged.set()
                return 'повреждённая фраза'
        worker = CaptureWorker(Settings(silence_ms=64, min_speech_ms=0), signals, LatencyMetrics(), 1, Capture, VAD, STT)
        worker.start()
        try:
            self.assertTrue(pump_until(lambda: bool(errors)))
            self.assertEqual(phrases, [])
        finally:
            worker.stop()
            self.assertTrue(pump_until(lambda: not worker.is_alive()))

    def test_capture_stays_open_between_phrases_and_closes(self):
        signals = WorkerSignals()
        phrases = []
        signals.phrase.connect(lambda epoch, phrase: phrases.append(phrase))
        class Capture:
            opened = closed = 0
            def __init__(self, inbox, device):
                self.inbox = inbox
            def open(self):
                Capture.opened += 1
                for i in range(8):
                    self.inbox.push(bytes([1 if i in (0, 4) else 0]) * 1024, perf_counter())
            def close(self):
                Capture.closed += 1
        class VAD:
            def __init__(self, path): pass
            def __call__(self, pcm, sample_rate):
                assert sample_rate == 16000
                return .9 if pcm[0] else 0
            def reset(self): pass
        class STT:
            def __init__(self, path): pass
            def reset(self): pass
            def feed(self, pcm): return 'привет'
            def finalize(self): return 'привет'
        worker = CaptureWorker(Settings(silence_ms=64, min_speech_ms=0), signals, LatencyMetrics(), 1,
                               Capture, VAD, STT)
        worker.start()
        try:
            self.assertTrue(pump_until(lambda: len(phrases) == 2))
            self.assertEqual(Capture.opened, 1)
            self.assertEqual(Capture.closed, 0)
            self.assertTrue(worker.is_alive())
            self.assertNotEqual(phrases[0]['id'], phrases[1]['id'])
        finally:
            worker.stop()
            self.assertTrue(pump_until(lambda: not worker.is_alive()))
        self.assertEqual(Capture.closed, 1)

    def test_controller_drops_late_result(self):
        controller = VoiceSessionController(Settings())
        token = controller.guard.begin('one')
        controller.cancel_response()
        controller._answer(token, 'late')
        controller._pcm(token, PCM(bytes(1024), 16000), perf_counter())
        self.assertEqual(controller.answer, '')
        self.assertIsNone(controller.playback.sink)
        controller.shutdown()
        controller._poll()

    def test_echo_overlap_and_tail(self):
        echo = EchoGuard()
        echo.start(1)
        self.assertTrue(echo.overlaps(.5, 1.5))
        echo.end(2)
        self.assertTrue(echo.overlaps(2.1, 2.2))
        self.assertFalse(echo.overlaps(2.3, 2.4))

    def test_pause_resume_keeps_buffer_position(self):
        from PyQt6.QtCore import QByteArray, QBuffer, QIODevice
        class Sink:
            def suspend(self): pass
            def resume(self): pass
        playback = PCMPlayback()
        playback.storage = QByteArray(bytes(1024))
        playback.buffer = QBuffer(playback.storage)
        playback.buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        playback.buffer.seek(100)
        playback.sink = Sink()
        playback.pause()
        self.assertEqual(playback.buffer.pos(), 100)
        playback.resume()
        self.assertEqual(playback.buffer.pos(), 100)
        playback.sink = None
        playback.stop()

    def test_dialog_thread_shutdown(self):
        class Provider:
            def __init__(self, settings): pass
            async def close(self): pass
            async def stream(self, messages, first_token):
                first_token()
                await asyncio.sleep(20)
                yield 'Ответ.'
        runtime = DialogRuntime(Settings(), WorkerSignals(), LatencyMetrics())
        with patch('alice_voice.workers.LLMProvider', Provider):
            runtime.start()
            try:
                self.assertTrue(runtime.ready.wait(3))
                self.assertTrue(runtime.submit(1, 'hello'))
                sleep(.02)
            finally:
                runtime.shutdown()
                self.assertTrue(pump_until(lambda: not runtime.is_alive()))

    def test_no_audio_file_operations_in_product(self):
        # Static guard complements byte-buffer integration tests.
        for path in Path('alice_voice').rglob('*.py'):
            text = path.read_text(encoding='utf-8')
            if path.name == 'cosmic_widgets.py':
                # QPainter.save() saves an in-memory transform, not an audio file.
                text = text.replace('p.save()', '')
            if path.name == 'focus_style.py':
                text = text.replace('painter.save()', '')
            for forbidden in ('tempfile', 'ffmpeg', 'ffplay', 'decode_file(', 'save_audio(', '.save('):
                if forbidden == 'tempfile' and path.name in ('config.py', 'silero_tts.py', 'owner_voice.py'):
                    continue  # Atomic settings/profile JSON or verified model weights; no audio.
                if forbidden == '.save(' and path.name in ('ui.py', 'settings_store.py', 'vision.py', 'desktop_backend.py'):
                    continue  # Settings JSON or JPEG encoded into BytesIO, never audio.
                self.assertNotIn(forbidden, text, path.name)


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_ollama_stream_first_token_full_reply(self):
        import httpx
        def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(payload['options']['num_predict'], 256)
            self.assertEqual(payload['messages'][0]['role'], 'system')
            return httpx.Response(200, content=b'{"message":{"content":"Hello "}}\n{"message":{"content":"world."}}\n')
        first = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = LLMProvider(Settings(), client)
            self.assertEqual(await provider.generate([{'role': 'user', 'content': 'hi'}], lambda: first.append(1)), 'Hello world.')
        self.assertEqual(first, [1])

    async def test_gemini_key_header_and_model_check(self):
        import httpx
        requests = []
        def handler(request):
            requests.append(request)
            self.assertNotIn('fake-test-key', str(request.url))
            self.assertEqual(request.headers['x-goog-api-key'], 'fake-test-key')
            if request.method == 'GET':
                return httpx.Response(200, json={'supportedGenerationMethods': ['generateContent']})
            return httpx.Response(200, content=b'data: {"candidates":[{"content":{"parts":[{"text":"Hello."}]}}]}\n\n')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = LLMProvider(Settings(provider='gemini'), client, key_loader=lambda: 'fake-test-key')
            self.assertEqual(await provider.generate([{'role': 'user', 'content': 'hi'}], lambda: None), 'Hello.')
        self.assertEqual([r.method for r in requests], ['GET', 'POST'])

    async def test_http_error_sanitized(self):
        import httpx
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(401, text='secret'))) as client:
            provider = LLMProvider(Settings(), client)
            with self.assertRaises(ProviderError) as error:
                await provider.generate([], lambda: None)
            self.assertNotIn('secret', str(error.exception))


if __name__ == '__main__':
    unittest.main()
