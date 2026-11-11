import tempfile
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from alice_voice.whisper_stt import WhisperRefiner, RefinementQueue


class WhisperAdapterTests(unittest.TestCase):
    def test_complete_pcm_is_normalized_and_transcribed_in_russian(self):
        native = Mock()
        native.transcribe.return_value = (iter([
            SimpleNamespace(text=' Найди погоду', no_speech_prob=.1, avg_logprob=-.2),
            SimpleNamespace(text=' в Бишкеке.', no_speech_prob=.1, avg_logprob=-.2)]), None)
        samples = np.array([-32768, 0, 16384, 0] * 512, dtype='<i2')
        refiner = WhisperRefiner(native=native)
        self.assertEqual(refiner.refine(samples.tobytes()), 'Найди погоду в Бишкеке.')
        audio = native.transcribe.call_args.args[0]
        np.testing.assert_allclose(audio, samples.astype(np.float32) / 32768)
        self.assertEqual(native.transcribe.call_args.kwargs['language'], 'ru')
        self.assertFalse(native.transcribe.call_args.kwargs['condition_on_previous_text'])

    def test_missing_local_model_reports_safe_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'Whisper'):
                WhisperRefiner(str(Path(directory) / 'missing'))

    def test_incomplete_model_never_calls_native_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'model.bin').write_bytes(b'weights')
            with patch('faster_whisper.WhisperModel') as loader:
                with self.assertRaises(RuntimeError): WhisperRefiner(directory)
                loader.assert_not_called()

    def test_cancelled_transcription_does_not_consume_remaining_segments(self):
        stop = Event(); visited = []
        def segments():
            visited.append('first'); stop.set()
            yield SimpleNamespace(text='old', no_speech_prob=.1, avg_logprob=-.2)
            visited.append('second')
            yield SimpleNamespace(text='old2', no_speech_prob=.1, avg_logprob=-.2)
        native = Mock(transcribe=Mock(return_value=(segments(), None)))
        self.assertEqual(WhisperRefiner(native=native).refine(bytes(1024), cancelled=stop.is_set), '')
        self.assertEqual(visited, ['first'])

    def test_silence_hallucination_is_rejected(self):
        native = Mock()
        native.transcribe.return_value = (iter([
            SimpleNamespace(text='Подписывайтесь!', no_speech_prob=.99, avg_logprob=-2.)]), None)
        self.assertEqual(WhisperRefiner(native=native).refine(bytes(1024)), '')


class RefinementQueueTests(unittest.TestCase):
    def test_cancellation_during_inference_discards_result(self):
        stop, entered, release = Event(), Event(), Event()
        emitted = []
        def refine(pcm, cancelled=None):
            entered.set(); release.wait(2); return 'старый запрос'
        queue = RefinementQueue(Mock(refine=refine), stop, lambda p: True, emitted.append, self.fail)
        queue.submit({'id': 'old'}, bytes(1024))
        self.assertTrue(entered.wait(1))
        stop.set(); release.set(); queue.close()
        self.assertFalse(emitted)

    def test_slow_refinement_is_bounded_and_stale_results_are_discarded(self):
        entered, release, stop = Event(), Event(), Event()
        emitted, failures = [], []
        valid = {'value': True}
        def refine(pcm, cancelled=None):
            entered.set()
            release.wait(3)
            return 'Найди погоду в Бишкеке'
        queue = RefinementQueue(Mock(refine=refine), stop,
                                lambda phrase: valid['value'], emitted.append, failures.append)
        try:
            self.assertTrue(queue.submit({'id': 'first'}, bytes(1024)))
            self.assertTrue(entered.wait(1))
            self.assertTrue(queue.submit({'id': 'second'}, bytes(1024)))
            self.assertFalse(queue.submit({'id': 'third'}, bytes(1024)))
            valid['value'] = False
        finally:
            release.set()
            queue.close()
        self.assertEqual(emitted, [])
        self.assertEqual(failures, [])

    def test_result_replaces_vosk_text_without_losing_identity_or_timing(self):
        emitted = []
        phrase = {'id': 'one', 'text': 'найти погода в печке', 'ready': 10.,
                  'generation': 4, 'revision': 5, 'speech_end': 9., 'acoustic': 'independent'}
        queue = RefinementQueue(Mock(refine=Mock(return_value='Найди погоду в Бишкеке')),
                                Event(), lambda p: True, emitted.append, self.fail)
        self.assertTrue(queue.submit(phrase, bytes(1024)))
        queue.close()
        self.assertEqual(emitted, [dict(phrase, text='Найди погоду в Бишкеке')])
        self.assertEqual(phrase['text'], 'найти погода в печке')

    def test_inference_error_has_no_silent_vosk_fallback(self):
        emitted, failures = [], []
        queue = RefinementQueue(Mock(refine=Mock(side_effect=ValueError('private detail'))),
                                Event(), lambda p: True, emitted.append, failures.append)
        queue.submit({'id': 'one', 'text': 'wrong'}, bytes(1024))
        queue.close()
        self.assertEqual(emitted, [])
        self.assertEqual(len(failures), 1)
        self.assertNotIn('private detail', failures[0])

    def test_stop_prevents_delivery(self):
        stop = Event(); stop.set()
        refiner = Mock(); emitted = []
        queue = RefinementQueue(refiner, stop, lambda p: True, emitted.append, self.fail)
        self.assertFalse(queue.submit({'id': 'one'}, bytes(1024)))
        queue.close()
        refiner.refine.assert_not_called()
        self.assertFalse(emitted)


class HybridCaptureTests(unittest.TestCase):
    def capture(self, raw_text, slow=False):
        from time import perf_counter, sleep
        from PyQt6.QtCore import QCoreApplication
        from alice_voice.config import Settings
        from alice_voice.metrics import LatencyMetrics
        from alice_voice.workers import CaptureWorker, WorkerSignals
        app = QCoreApplication.instance() or QCoreApplication([])
        sequence = [0] * 2 + [2] * 18 + [0] * 3
        frames = [value.to_bytes(2, 'little') * 512 for value in sequence]
        signals = WorkerSignals(); phrases = []; errors = []
        signals.phrase.connect(lambda epoch, p: phrases.append(p))
        signals.error.connect(lambda epoch, e: errors.append(e))
        class Capture:
            def __init__(self, inbox, device): self.inbox = inbox
            def open(self):
                base = perf_counter()
                for index, frame in enumerate(frames):
                    self.inbox.push(frame, base + (index + 1) * .032)
            def close(self): pass
        class Vad:
            def __init__(self, path): pass
            def __call__(self, pcm, rate): return .9 if pcm[0] else 0.
            def reset(self): pass
        class Stt:
            def __init__(self, path): pass
            def feed(self, pcm): return raw_text
            def finalize(self): return raw_text
            def reset(self): pass
        entered, release = Event(), Event()
        def refine(pcm, cancelled=None):
            entered.set()
            if slow: release.wait(3)
            return 'Найди погоду в Бишкеке'
        refiner = Mock(refine=Mock(side_effect=refine))
        worker = CaptureWorker(Settings(stt_backend='hybrid', silence_ms=64, preroll_ms=64),
                               signals, LatencyMetrics(), 1, Capture, Vad, Stt)
        with patch('alice_voice.whisper_stt.WhisperRefiner', return_value=refiner):
            worker.start()
            try:
                if slow:
                    self.assertTrue(entered.wait(1))
                    deadline = perf_counter() + 1
                    while worker.inbox.size and perf_counter() < deadline: sleep(.005)
                    self.assertEqual(worker.inbox.size, 0, 'Whisper blocked capture')
                    release.set()
                deadline = perf_counter() + 3
                while not phrases and not errors and perf_counter() < deadline:
                    app.processEvents(); sleep(.005)
            finally:
                release.set()
                worker.stop(); worker.join(3); app.processEvents()
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(phrases)
        return phrases, refiner, b''.join(frames[:-1])

    def test_final_text_uses_whisper_with_full_preroll_and_endpoint(self):
        phrases, refiner, expected_pcm = self.capture('найти погода в печке')
        self.assertEqual(phrases[0]['text'], 'Найди погоду в Бишкеке')
        self.assertEqual(refiner.refine.call_args.args[0], expected_pcm)

    def test_direct_stop_does_not_wait_for_whisper(self):
        phrases, refiner, _ = self.capture('стоп')
        self.assertEqual(phrases[0]['text'], 'стоп')
        refiner.refine.assert_not_called()

    def test_capture_drains_frames_while_whisper_is_busy(self):
        phrases, _, _ = self.capture('найти погода в печке', slow=True)
        self.assertEqual(phrases[0]['text'], 'Найди погоду в Бишкеке')


class HybridSettingsTests(unittest.TestCase):
    def test_legacy_default_and_invalid_backend(self):
        from alice_voice.config import Settings
        self.assertEqual(Settings.from_dict({}).stt_backend, 'vosk')
        self.assertEqual(Settings(stt_backend='hybrid').validate().stt_backend, 'hybrid')
        with self.assertRaises(ValueError): Settings(stt_backend='remote').validate()
