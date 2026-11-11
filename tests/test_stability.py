"""Stage 1 regressions: configuration and partial worker initialization."""
import asyncio
from dataclasses import fields, replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import QCoreApplication

from alice_voice.config import Settings
from alice_voice.controller import VoiceSessionController
from alice_voice.metrics import LatencyMetrics
from alice_voice.workers import DialogRuntime, WorkerSignals


APP = QCoreApplication.instance() or QCoreApplication([])


class SettingsStabilityTests(unittest.TestCase):
    def test_every_field_rejects_wrong_types(self):
        defaults = Settings()
        for field in fields(defaults):
            value = getattr(defaults, field.name)
            if isinstance(value, bool):
                invalid = (0, 1, 'false', None, [])
            elif isinstance(value, str):
                invalid = (123, True, None, [], {})
            elif field.type is int:
                invalid = (1.5, True, '1', None, [], {})
            elif field.type is float:
                invalid = (True, '0.5', None, [], {}, float('nan'), float('inf'))
            else:  # optional microphone index
                invalid = (1.5, True, '1', [], {}, -1)
            for item in invalid:
                with self.subTest(field=field.name, type=type(item).__name__):
                    with self.assertRaises(ValueError):
                        replace(defaults, **{field.name: item}).validate()

    def test_json_root_must_be_object(self):
        for payload in (None, True, 42, 1.5, [], [[], {}], ''):
            with self.subTest(payload=repr(payload)):
                with self.assertRaises(ValueError):
                    Settings.from_dict(payload)

    def test_existing_settings_and_integer_real_values_are_supported(self):
        self.assertEqual(Settings.from_dict(Settings().to_dict()), Settings())
        self.assertEqual(Settings.from_dict({'volume': 1, 'timeout_s': 30}).volume, 1)
        self.assertEqual(Settings.from_dict({'input_device': 0}).input_device, 0)

    def test_bad_url_port_is_rejected_without_echoing_value(self):
        with self.assertRaises(ValueError) as caught:
            Settings(ollama_url='http://localhost:private-value').validate()
        self.assertNotIn('private-value', str(caught.exception))

    def test_atomic_roundtrip_and_no_temporary_file_left(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            Settings().save(path)
            updated = Settings(system_prompt='Русский текст', volume=.4)
            updated.save(path)
            self.assertEqual(Settings.load(path), updated)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_replace_failure_preserves_original_and_removes_temp_file(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            Settings().save(path)
            original = path.read_bytes()
            with patch('os.replace', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    Settings(volume=.3).save(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_flush_failure_preserves_original_and_cleans_up(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            Settings().save(path)
            original = path.read_bytes()
            with patch('os.fsync', side_effect=OSError('flush failure')):
                with self.assertRaises(OSError):
                    Settings(volume=.3).save(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_invalid_settings_never_touch_existing_file(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            path.write_text('{}', encoding='utf-8')
            with self.assertRaises(ValueError):
                Settings(history_turns=1.5).save(path)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {})


class RuntimeStabilityTests(unittest.TestCase):
    def setUp(self):
        self.signals = WorkerSignals()
        self.errors = []
        self.signals.failed.connect(lambda token, message: self.errors.append((token, message)))
        self.runtime = DialogRuntime(Settings(), self.signals, LatencyMetrics())

    def test_loop_creation_failure_is_reported_without_leaking_exception(self):
        with patch('alice_voice.workers.asyncio.new_event_loop', side_effect=OSError('private-value')):
            self.runtime.run()
        self.assertFalse(self.runtime.ready.is_set())
        self.assertIsNone(self.runtime.loop)
        self.assertEqual(self.errors[0][0], -1)
        self.assertNotIn('private-value', self.errors[0][1])

    def test_executor_creation_failure_closes_loop(self):
        loop = asyncio.new_event_loop()
        with patch('alice_voice.workers.asyncio.new_event_loop', return_value=loop), \
                patch('alice_voice.workers.ThreadPoolExecutor', side_effect=OSError('private-value')):
            self.runtime.run()
        self.assertTrue(loop.is_closed())
        self.assertFalse(self.runtime.ready.is_set())
        self.assertTrue(self.errors)

    def test_provider_cleanup_failure_does_not_skip_executor_or_loop(self):
        loop = asyncio.new_event_loop()
        executor = Mock()
        class Provider:
            def __init__(self, settings): pass
            async def close(self): raise OSError('private-value')
        with patch('alice_voice.workers.asyncio.new_event_loop', return_value=loop), \
                patch('alice_voice.workers.ThreadPoolExecutor', return_value=executor), \
                patch('alice_voice.workers.LLMProvider', Provider), \
                patch('alice_voice.workers.SAPISynthesizer', side_effect=OSError('initialization')):
            self.runtime.run()
        executor.shutdown.assert_called_once_with(wait=True, cancel_futures=True)
        self.assertTrue(loop.is_closed())
        self.assertFalse(self.runtime.ready.is_set())

    def test_executor_cleanup_failure_does_not_skip_loop(self):
        loop = asyncio.new_event_loop()
        executor = Mock()
        executor.shutdown.side_effect = OSError('private-value')
        with patch('alice_voice.workers.asyncio.new_event_loop', return_value=loop), \
                patch('alice_voice.workers.ThreadPoolExecutor', return_value=executor), \
                patch('alice_voice.workers.LLMProvider', side_effect=OSError('initialization')):
            self.runtime.run()
        self.assertTrue(loop.is_closed())
        self.assertFalse(self.runtime.ready.is_set())

    def test_cancelled_provider_cleanup_still_closes_loop_and_executor(self):
        loop = asyncio.new_event_loop()
        executor = Mock()
        class Provider:
            def __init__(self, settings): pass
            async def close(self): raise asyncio.CancelledError
        with patch('alice_voice.workers.asyncio.new_event_loop', return_value=loop), \
                patch('alice_voice.workers.ThreadPoolExecutor', return_value=executor), \
                patch('alice_voice.workers.LLMProvider', Provider), \
                patch('alice_voice.workers.SAPISynthesizer', side_effect=OSError('initialization')):
            self.runtime.run()
        executor.shutdown.assert_called_once()
        self.assertTrue(loop.is_closed())

    def test_task_creation_failure_is_reported_and_coroutine_closed(self):
        self.runtime.loop = Mock()
        self.runtime.loop.create_task.side_effect = RuntimeError('private-value')
        self.runtime._start(3, 'hello')
        self.assertEqual(self.errors[0][0], 3)
        self.assertNotIn('private-value', self.errors[0][1])
        coroutine = self.runtime.loop.create_task.call_args.args[0]
        self.assertIsNone(coroutine.cr_frame)

    def test_closed_loop_race_does_not_escape_submit_cancel_or_shutdown(self):
        loop = asyncio.new_event_loop()
        loop.close()
        self.runtime.loop = loop
        self.runtime.ready.set()
        with patch.object(self.runtime, 'is_alive', return_value=True):
            self.assertFalse(self.runtime.submit(1, 'hello'))
            self.runtime.cancel()
            self.runtime.shutdown()

    def test_shutdown_during_initialization_finishes_worker(self):
        entered, release = Event(), Event()
        class Provider:
            def __init__(self, settings):
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('test initialization timeout')
            async def close(self): pass
        with patch('alice_voice.workers.LLMProvider', Provider):
            self.runtime.start()
            try:
                self.assertTrue(entered.wait(3))
                self.runtime.shutdown()
            finally:
                release.set()
                self.runtime.join(3)
        self.assertFalse(self.runtime.is_alive())
        self.assertFalse(self.runtime.ready.is_set())


class ControllerStartupTests(unittest.TestCase):
    def setUp(self):
        self.controller = VoiceSessionController(Settings())
        self.messages = []
        self.controller.notification.connect(self.messages.append)

    def tearDown(self):
        self.controller.capture = self.controller.runtime = None
        self.controller.shutdown()
        self.controller._poll()

    def test_dialog_thread_start_failure_is_recoverable(self):
        with patch('alice_voice.controller.DialogRuntime.start', side_effect=OSError('private-value')):
            self.controller.enable_microphone()
        self.assertIsNone(self.controller.capture)
        self.assertIsNone(self.controller.runtime)
        self.assertEqual(self.controller.mic_state, 'error')
        self.assertTrue(self.messages)
        self.assertNotIn('private-value', self.messages[-1])

    def test_capture_start_failure_shuts_down_existing_runtime(self):
        runtime = Mock(shutdown_requested=False)
        self.controller.runtime = runtime
        with patch('alice_voice.controller.CaptureWorker.start', side_effect=OSError('private-value')):
            self.controller.enable_microphone()
        self.assertIsNone(self.controller.capture)
        runtime.shutdown.assert_called_once()
        self.assertEqual(self.controller.mic_state, 'error')

    def test_fatal_runtime_failure_stops_capture_and_cancels_dialog(self):
        capture = Mock()
        self.controller.capture = capture
        self.controller.guard.begin('active')
        self.controller._failed(-1, 'service unavailable')
        capture.stop.assert_called_once()
        self.assertIsNone(self.controller.guard.active)
        self.assertEqual(self.controller.mic_state, 'error')

    def test_late_loading_signal_cannot_undo_startup_error(self):
        self.controller.capture = Mock()
        self.controller.capture.stop_event.is_set.return_value = True
        self.controller.mic_state = 'error'
        self.controller._mic(self.controller.epoch, 'loading')
        self.assertEqual(self.controller.mic_state, 'error')


if __name__ == '__main__':
    unittest.main()
