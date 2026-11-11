"""Screen capture and Gemini vision contracts without reading the desktop."""
import asyncio
import base64
from io import BytesIO
import json
from threading import Event, get_ident
from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
from PyQt6.QtCore import QCoreApplication

from alice_voice.config import Settings
from alice_voice.controller import VoiceSessionController
from alice_voice.metrics import LatencyMetrics
from alice_voice.providers import LLMProvider, ProviderError
from alice_voice.speech import PCM
from alice_voice.workers import DialogRuntime, WorkerSignals

APP = QCoreApplication.instance() or QCoreApplication([])


def jpeg():
    from PIL import Image
    with Image.new('RGB', (8, 4), 'red') as image, BytesIO() as buffer:
        image.save(buffer, format='JPEG')
        return buffer.getvalue()


class CaptureTests(unittest.TestCase):
    def test_preview_selection_maps_to_physical_pixels_with_negative_origin(self):
        from alice_voice.utils.vision import map_preview_region
        native = {'left': -1920, 'top': 0, 'width': 1920, 'height': 1080}
        result = map_preview_region((0, 50, 960, 540), (240, 185, 480, 270), native)
        self.assertEqual(result, {'left': -1440, 'top': 270, 'width': 960, 'height': 540})
        with self.assertRaises(ValueError):
            map_preview_region((0, 50, 960, 540), (0, 0, 20, 20), native)

    def test_selected_region_is_captured_without_capturing_other_monitors(self):
        from alice_voice.utils.vision import capture_screen_jpeg
        capture = Mock()
        region = {'left': -900, 'top': 100, 'width': 600, 'height': 300}
        capture.grab.return_value = SimpleNamespace(size=(600, 300), rgb=b'\x01\x02\x03' * 180000)
        manager = Mock(__enter__=Mock(return_value=capture), __exit__=Mock(return_value=False))
        with patch('mss.mss', return_value=manager):
            data = capture_screen_jpeg(region=region)
        from PIL import Image
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.size, (600, 300))
        capture.grab.assert_called_once_with(region)
        manager.__exit__.assert_called_once()

    def test_monitor_list_contains_physical_bounds_and_primary_flag(self):
        from alice_voice.utils.vision import list_capture_monitors
        monitors = [{'left': -1920, 'top': 0, 'width': 3840, 'height': 1080},
                    {'left': 0, 'top': 0, 'width': 1920, 'height': 1080, 'is_primary': True},
                    {'left': -1920, 'top': 0, 'width': 1920, 'height': 1080, 'is_primary': False}]
        capture = SimpleNamespace(monitors=monitors)
        manager = Mock(__enter__=Mock(return_value=capture), __exit__=Mock(return_value=False))
        with patch('mss.mss', return_value=manager):
            result = list_capture_monitors()
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]['left'], -1920)
        self.assertTrue(result[0]['is_primary'])
        manager.__exit__.assert_called_once()

    def test_capture_resizes_and_closes_mss_on_repeated_calls(self):
        from PIL import Image
        from alice_voice.utils.vision import capture_screen_jpeg
        instances = []
        class Capture:
            primary_monitor = {'width': 1920, 'height': 1080, 'left': 0, 'top': 0}
            def __enter__(self):
                self.closed = False
                instances.append(self)
                return self
            def __exit__(self, *args): self.closed = True
            def grab(self, monitor):
                self.selected = monitor
                return SimpleNamespace(size=(1920, 1080), rgb=b'\x10\x20\x30' * (1920 * 1080))
        with patch('mss.mss', Capture):
            for _ in range(4):
                data = capture_screen_jpeg()
                self.assertLess(len(data), 2 * 1024 * 1024)
                with Image.open(BytesIO(data)) as image:
                    self.assertEqual(image.format, 'JPEG')
                    self.assertEqual(image.size, (1280, 720))
        self.assertTrue(all(instance.closed for instance in instances))
        self.assertTrue(all(instance.selected is instance.primary_monitor for instance in instances))

    def test_capture_failure_closes_handles_and_hides_native_error(self):
        from alice_voice.utils.vision import capture_screen_jpeg
        capture = Mock()
        capture.primary_monitor = {'width': 100, 'height': 100}
        capture.grab.side_effect = OSError('private desktop details')
        manager = Mock(__enter__=Mock(return_value=capture), __exit__=Mock(return_value=False))
        with patch('mss.mss', return_value=manager):
            with self.assertRaises(RuntimeError) as error:
                capture_screen_jpeg()
        self.assertNotIn('private desktop details', str(error.exception))
        manager.__exit__.assert_called_once()

    def test_screen_command_requires_explicit_prefix(self):
        from alice_voice.utils.vision import is_screen_request
        self.assertTrue(is_screen_request('Посмотри на экран и объясни ошибку'))
        self.assertTrue(is_screen_request('что на экране?'))
        self.assertFalse(is_screen_request('не посмотри на экран'))
        self.assertFalse(is_screen_request('что означает фраза «посмотри на экран»'))


class VisionHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_gemini_gets_image_only_on_current_turn_and_messages_are_unchanged(self):
        payloads = []
        def handler(request):
            if request.method == 'GET':
                return httpx.Response(200, json={'supportedGenerationMethods': ['generateContent']})
            payloads.append(json.loads(request.content))
            return httpx.Response(200, content=b'data: {"candidates":[{"content":{"parts":[{"text":"Screen."}]}}]}\n\n')
        messages = [{'role': 'user', 'content': 'before'}, {'role': 'assistant', 'content': 'reply'},
                    {'role': 'user', 'content': 'explain screen'}]
        data = jpeg()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = LLMProvider(Settings(provider='gemini'), client, key_loader=lambda: 'fake')
            self.assertEqual(await provider.generate(messages, lambda: None, image_jpeg=data), 'Screen.')
            await provider.generate(messages, lambda: None)
        parts = payloads[0]['contents'][-1]['parts']
        self.assertEqual(parts[0], {'text': 'explain screen'})
        self.assertEqual(parts[1], {'inline_data': {'mime_type': 'image/jpeg',
                                                 'data': base64.b64encode(data).decode('ascii')}})
        self.assertEqual(len(payloads[0]['contents'][0]['parts']), 1)
        self.assertEqual(len(payloads[1]['contents'][-1]['parts']), 1)
        self.assertTrue(all(set(message) == {'role', 'content'} for message in messages))

    async def test_unsupported_provider_rejects_image_without_http(self):
        requests = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: requests.append(r))) as client:
            with self.assertRaises(ProviderError):
                await LLMProvider(Settings(), client).generate(
                    [{'role': 'user', 'content': 'screen'}], lambda: None, image_jpeg=jpeg())
        self.assertEqual(requests, [])

    async def test_runtime_captures_off_event_loop_and_keeps_history_text_only(self):
        data = jpeg()
        threads, images, errors = [], [], []
        class Provider:
            async def stream(self, messages, first, image_jpeg=None):
                images.append(image_jpeg)
                yield 'На экране окно.'
        class Synth:
            async def synthesize(self, text, cancel): return PCM(bytes(64), 16000)
        signals = WorkerSignals()
        runtime = DialogRuntime(Settings(provider='gemini'), signals, LatencyMetrics())
        runtime.provider, runtime.synth = Provider(), Synth()
        signals.pcm.connect(lambda token, pcm, when, sequence: runtime._release_pcm(token, sequence))
        signals.failed.connect(lambda token, message: errors.append(message))
        def capture():
            threads.append(get_ident())
            return data
        with patch('alice_voice.workers.capture_screen_jpeg', side_effect=capture):
            await asyncio.wait_for(runtime._process(1, 'screen', Event(), capture_screen=True), 2)
        self.assertEqual(errors, [])
        self.assertEqual(images, [data])
        self.assertNotEqual(threads[0], get_ident())
        self.assertEqual(list(runtime.history), [{'role': 'user', 'content': 'screen'},
                                               {'role': 'assistant', 'content': 'На экране окно.'}])
        self.assertIsNone(runtime.stream_session)

    async def test_cancel_during_capture_never_sends_image(self):
        entered, release, cancel = Event(), Event(), Event()
        runtime = DialogRuntime(Settings(provider='gemini'), WorkerSignals(), LatencyMetrics())
        runtime.provider = Mock()
        def capture():
            entered.set()
            release.wait(2)
            return jpeg()
        with patch('alice_voice.workers.capture_screen_jpeg', side_effect=capture):
            task = asyncio.create_task(runtime._process(1, 'screen', cancel, capture_screen=True))
            try:
                for _ in range(100):
                    if entered.is_set(): break
                    await asyncio.sleep(.005)
                self.assertTrue(entered.is_set())
                cancel.set()
                release.set()
                await asyncio.wait_for(task, 2)
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        runtime.provider.stream.assert_not_called()
        self.assertEqual(list(runtime.history), [])


class ScreenControllerTests(unittest.TestCase):
    def setUp(self):
        self.controller = VoiceSessionController(Settings(provider='gemini'))
        self.runtime = Mock()
        self.runtime.ready = Event()
        self.runtime.ready.set()
        self.runtime.shutdown_requested = False
        self.runtime.submit.return_value = True
        self.controller.runtime = self.runtime

    def tearDown(self):
        self.controller.runtime = None
        self.controller.shutdown()
        self.controller._poll()

    def test_button_can_start_dialog_without_microphone_and_cannot_queue_twice(self):
        self.controller.request_screen()
        self.runtime.submit.assert_called_once()
        self.assertTrue(self.runtime.submit.call_args.kwargs['capture_screen'])
        self.assertIsNone(self.controller.capture)
        self.controller.request_screen()
        self.runtime.submit.assert_called_once()

    def test_cancel_drops_request_waiting_for_runtime(self):
        self.runtime.ready.clear()
        self.controller.request_screen()
        self.runtime.submit.assert_not_called()
        self.controller.cancel_response()
        self.runtime.ready.set()
        self.controller._poll()
        self.runtime.submit.assert_not_called()

    def test_nonvision_provider_does_not_capture_or_send(self):
        self.controller.settings = Settings(provider='ollama')
        self.controller.request_screen()
        self.runtime.submit.assert_not_called()
        self.assertIsNone(self.controller.guard.active)

    def test_voice_command_preserves_prompt_and_deduplicates_stt_event(self):
        previews = []
        self.controller.screen_preview_requested.connect(lambda prompt, identity: previews.append((prompt, identity)))
        self.controller.mic_state = 'listening'
        now = perf_counter()
        phrase = {'id': 'vision-voice', 'text': 'Посмотри на экран и объясни ошибку',
                  'start': now - 1, 'speech_end': now - .3, 'ready': now}
        self.controller._phrase(0, phrase)
        self.assertEqual(previews, [(phrase['text'], phrase['id'])])
        self.runtime.submit.assert_not_called()
        self.controller.screen_preview_active = False
        self.controller._phrase(0, phrase)
        self.assertEqual(len(previews), 1)

    def test_preview_jpeg_is_sent_without_recapturing_screen(self):
        data = jpeg()
        self.controller.request_screen('Объясни ошибку', image_jpeg=data)
        self.assertEqual(self.runtime.submit.call_args.kwargs, {'image_jpeg': data})
        self.assertIsNone(self.controller.pending_screen)

    def test_cancel_releases_image_queued_before_runtime_is_ready(self):
        self.runtime.ready.clear()
        self.controller.request_screen('Что здесь?', image_jpeg=jpeg())
        self.controller.cancel_response()
        self.assertIsNone(self.controller.pending_screen)
        self.runtime.ready.set()
        self.controller._poll()
        self.runtime.submit.assert_not_called()
