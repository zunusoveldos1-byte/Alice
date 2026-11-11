"""Capture-worker integration with deterministic audio, no microphone."""
from time import perf_counter, sleep
import unittest

from PyQt6.QtCore import QCoreApplication
from alice_voice.config import Settings
from alice_voice.metrics import LatencyMetrics
from alice_voice.workers import CaptureWorker, WorkerSignals


APP = QCoreApplication.instance() or QCoreApplication([])


class WorkerSpeechGateTests(unittest.TestCase):
    def test_short_stop_is_recognized_only_in_active_response_mode(self):
        from threading import Event
        for responding, control_pending in ((False,False), (True,False), (False,True)):
            with self.subTest(responding=responding, control_pending=control_pending):
                signals = WorkerSignals()
                phrases = []
                signals.phrase.connect(lambda epoch, phrase: phrases.append(phrase))
                class Capture:
                    def __init__(self, inbox, device): self.inbox = inbox
                    def open(self):
                        for index in range(12):
                            self.inbox.push((b'\x02\x00' if index < 8 else b'\x00\x00') * 512,
                                            perf_counter() + index * .032)
                    def close(self): pass
                class VAD:
                    def __init__(self, path): pass
                    def __call__(self, pcm, rate): return .9 if pcm[0] else 0
                    def reset(self): pass
                class STT:
                    def __init__(self, path): pass
                    def reset(self): pass
                    def feed(self, pcm): return 'стоп'
                    def finalize(self): return 'стоп'
                worker = CaptureWorker(Settings(silence_ms=64,min_speech_ms=320), signals, LatencyMetrics(),
                                       1, Capture, VAD, STT)
                worker.response_active = Event()
                if responding: worker.response_active.set()
                if control_pending: worker.control_active.set()
                worker.start()
                try:
                    deadline = perf_counter() + .4
                    while perf_counter() < deadline:
                        APP.processEvents()
                        sleep(.005)
                    self.assertEqual([phrase['text'] for phrase in phrases], ['стоп'] if responding or control_pending else [])
                finally:
                    worker.stop()
                    worker.join(3)

    def test_echo_reference_keeps_contiguous_pcm_across_short_vad_gaps(self):
        import numpy as np
        from alice_voice.speech import PCM
        from alice_voice.utils.barge_in import PlaybackReference
        rng = np.random.default_rng(7)
        pattern = ([True] * 4 + [False] * 2) * 4 + [False] * 3
        frames = [rng.integers(-8000, 8000, 512, dtype=np.int16).tobytes()
                  if voiced else bytes(1024) for voiced in pattern]
        base = perf_counter()
        reference = PlaybackReference()
        reference.add(PCM(b''.join(frames), 16000), base)
        signals = WorkerSignals()
        evidence, phrases = [], []
        signals.voice_evidence.connect(lambda epoch, event: evidence.append(event))
        signals.phrase.connect(lambda *args: phrases.append(args))
        class Capture:
            def __init__(self, inbox, device): self.inbox = inbox
            def open(self):
                for index, pcm in enumerate(frames):
                    self.inbox.push(pcm, base + (index + 1) * .032)
            def close(self): pass
        class VAD:
            def __init__(self, path): self.index = 0
            def __call__(self, pcm, rate):
                value = pattern[self.index]
                self.index += 1
                return .9 if value else 0
            def reset(self): pass
        class STT:
            def __init__(self, path): pass
            def reset(self): pass
            def feed(self, pcm): return 'ошибочная расшифровка'
            def finalize(self): return 'ошибочная расшифровка'
        worker = CaptureWorker(Settings(silence_ms=96, min_speech_ms=0, preroll_ms=0),
                               signals, LatencyMetrics(), 1, Capture, VAD, STT)
        worker.playback_reference = reference
        worker.start()
        try:
            deadline = perf_counter() + 3
            while not phrases and perf_counter() < deadline:
                APP.processEvents()
                sleep(.005)
            APP.processEvents()
            self.assertTrue(phrases)
            self.assertTrue(evidence)
            self.assertNotIn('independent', [event['acoustic'] for event in evidence])
        finally:
            worker.stop()
            worker.join(3)

    def test_stable_stt_evidence_preserves_preroll_and_final_phrase(self):
        sequence = [0] * 2 + [2] * 18 + [0] * 3
        signals = WorkerSignals()
        evidence, phrases, errors, received = [], [], [], []
        signals.voice_evidence.connect(lambda epoch, event: evidence.append(event))
        signals.phrase.connect(lambda epoch, phrase: phrases.append(phrase))
        signals.error.connect(lambda epoch, message: errors.append(message))
        class Capture:
            def __init__(self, inbox, device): self.inbox = inbox
            def open(self):
                for index, value in enumerate(sequence):
                    self.inbox.push(value.to_bytes(2, 'little') * 512, perf_counter() + index * .032)
            def close(self): pass
        class VAD:
            def __init__(self, path): pass
            def __call__(self, pcm, rate): return .9 if pcm[0] else 0
            def reset(self): pass
        class STT:
            def __init__(self, path): pass
            def reset(self): pass
            def feed(self, pcm):
                received.append(pcm[0])
                return 'найди погоду'
            def finalize(self): return 'найди погоду в Бишкеке'
        worker = CaptureWorker(Settings(silence_ms=64, preroll_ms=64), signals,
                               LatencyMetrics(), 1, Capture, VAD, STT)
        worker.start()
        try:
            deadline = perf_counter() + 3
            while not phrases and not errors and perf_counter() < deadline:
                APP.processEvents()
                sleep(.005)
            APP.processEvents()
            self.assertEqual(errors, [])
            self.assertTrue(evidence)
            self.assertTrue(evidence[0]['stable'])
            self.assertEqual(evidence[0]['id'], phrases[0]['id'])
            self.assertEqual(evidence[0]['start'], phrases[0]['start'])
            self.assertEqual(phrases[0]['text'], 'найди погоду в Бишкеке')
            self.assertEqual(received[:2], [0, 0])
            self.assertEqual(received.count(2), 18)
            self.assertEqual(worker.generation, 0)
        finally:
            worker.stop()
            worker.join(3)

    def test_vad_with_empty_stt_never_emits_interruption_evidence(self):
        signals = WorkerSignals()
        evidence, phrases = [], []
        signals.voice_evidence.connect(lambda *args: evidence.append(args))
        signals.phrase.connect(lambda *args: phrases.append(args))
        class Capture:
            def __init__(self, inbox, device): self.inbox = inbox
            def open(self):
                for index in range(24):
                    self.inbox.push((b'\x02\x00' if index < 20 else b'\x00\x00') * 512,
                                    perf_counter() + index * .032)
            def close(self): pass
        class VAD:
            def __init__(self, path): pass
            def __call__(self, pcm, rate): return .9 if pcm[0] else 0
            def reset(self): pass
        class STT:
            def __init__(self, path): pass
            def reset(self): pass
            def feed(self, pcm): return ''
            def finalize(self): return ''
        worker = CaptureWorker(Settings(silence_ms=64), signals, LatencyMetrics(),
                               1, Capture, VAD, STT)
        worker.start()
        try:
            deadline = perf_counter() + 3
            while not phrases and perf_counter() < deadline:
                APP.processEvents()
                sleep(.005)
            self.assertTrue(phrases)
            self.assertEqual(evidence, [])
        finally:
            worker.stop()
            worker.join(3)

    def test_click_discard_vosk_resets_and_continuous_vad_state(self):
        sequence = [1, 0, 0] + [2] * 10 + [0, 0] + [3] * 10 + [0, 0]
        signals = WorkerSignals()
        phrases, errors, received, resets = [], [], [], []
        signals.phrase.connect(lambda epoch, phrase: phrases.append(phrase))
        signals.error.connect(lambda epoch, error: errors.append(error))
        class Capture:
            def __init__(self, inbox, device): self.inbox = inbox
            def open(self):
                for index, value in enumerate(sequence):
                    self.inbox.push(value.to_bytes(2, 'little') * 512, perf_counter() + index * .032)
            def close(self): pass
        class VAD:
            def __init__(self, path): pass
            def __call__(self, pcm, sample_rate):
                assert sample_rate == 16000
                return .9 if pcm[0] else 0
            def reset(self): resets.append('vad')
        class STT:
            def __init__(self, path): self.fresh = False
            def reset(self):
                self.fresh = True
                resets.append('stt')
            def feed(self, pcm):
                if not self.fresh:
                    raise AssertionError('Vosk did not reset before a phrase')
                received.append(pcm[0])
                return 'привет'
            def finalize(self):
                self.fresh = False
                return 'привет'
        worker = CaptureWorker(Settings(silence_ms=64, preroll_ms=0), signals,
                               LatencyMetrics(), 1, Capture, VAD, STT)
        worker.start()
        try:
            deadline = perf_counter() + 3
            while len(phrases) < 2 and not errors and perf_counter() < deadline:
                APP.processEvents()
                sleep(.005)
            APP.processEvents()
            self.assertEqual(errors, [])
            self.assertEqual(len(phrases), 2)
            self.assertNotIn(1, received, 'click reached Vosk')
            self.assertEqual(received.count(2), 10)
            self.assertEqual(received.count(3), 10)
            self.assertNotIn('vad', resets, 'ordinary phrases reset streaming Silero state')
        finally:
            worker.stop()
            worker.join(3)
            APP.processEvents()
        self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main()
