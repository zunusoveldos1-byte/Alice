import inspect
import json
import unittest

from alice_voice.audio import AudioCapture, AudioFrame, FrameInbox, RATE, FRAME_SAMPLES
from alice_voice.recognition import PhraseSegmenter, VoskRecognizer, is_stop_command
from alice_voice.metrics import LatencyMetrics
from alice_voice.config import Settings


class FakeRecognizer:
    def __init__(self):
        self.received = []
        self.index = 0
    def AcceptWaveform(self, pcm):
        self.received.append(pcm)
        self.index += 1
        return self.index == 2
    def Result(self):
        return json.dumps({'text': 'первая часть'})
    def PartialResult(self):
        return json.dumps({'partial': 'часть'})
    def FinalResult(self):
        return json.dumps({'text': 'вторая часть'})
    def Reset(self):
        self.index = 0


class CoreTests(unittest.TestCase):
    def test_pcm_contract_and_callback(self):
        inbox = FrameInbox()
        capture = AudioCapture(inbox)
        capture.callback(bytes(1024), 512, None, None)
        self.assertEqual(RATE, 16000)
        self.assertEqual(FRAME_SAMPLES, 512)
        self.assertEqual(len(inbox.get().pcm), 1024)
        source = inspect.getsource(AudioCapture.callback)
        for forbidden in ('AcceptWaveform', 'predict', 'open(', 'emit(', 'requests'):
            self.assertNotIn(forbidden, source)

    def test_bounded_overflow_and_reset(self):
        inbox = FrameInbox()
        for _ in range(33):
            inbox.push(bytes(1024), 1.0)
        self.assertEqual(inbox.size, 32)
        self.assertTrue(inbox.damaged.is_set())
        inbox.clear()
        self.assertEqual(inbox.size, 0)

    def test_vosk_original_bytes_and_segments(self):
        fake = FakeRecognizer()
        rec = VoskRecognizer(native=fake)
        pcm = bytes(range(256)) * 4
        rec.feed(pcm)
        rec.feed(pcm)
        self.assertIs(fake.received[0], pcm)
        self.assertEqual(rec.finalize(), 'первая часть вторая часть')

    def test_preroll_endpoint_and_unique_ids(self):
        seg = PhraseSegmenter(silence_ms=64, preroll_ms=320, min_speech_ms=0)
        for i in range(12):
            self.assertEqual(seg.feed(AudioFrame(bytes(1024), i * .032), 0).frames, [])
        start = seg.feed(AudioFrame(b'x' * 1024, .384), .9)
        self.assertTrue(start.started)
        self.assertEqual(len(start.frames), 11)
        self.assertEqual(start.frames[-1].pcm, b'x' * 1024)
        seg.feed(AudioFrame(bytes(1024), .416), 0)
        end = seg.feed(AudioFrame(bytes(1024), .448), 0)
        self.assertTrue(end.finished)
        previous = end.utterance_id
        next_start = seg.feed(AudioFrame(bytes(1024), .480), .9)
        self.assertNotEqual(previous, next_start.utterance_id)

    def test_preroll_uses_actual_rounded_audio_start(self):
        seg = PhraseSegmenter(preroll_ms=33, min_speech_ms=0)
        seg.feed(AudioFrame(bytes(1024), 1.936), 0)
        seg.feed(AudioFrame(bytes(1024), 1.968), 0)
        update = seg.feed(AudioFrame(bytes(1024), 2.0), .9)
        self.assertAlmostEqual(update.audio_start, 1.904)

    def test_exact_stop_commands(self):
        for text in ('Отключайся!', 'Выключи микрофон', ' отключи микрофон. '):
            self.assertTrue(is_stop_command(text))
        self.assertFalse(is_stop_command('Что означает слово отключайся?'))
        self.assertFalse(is_stop_command('пожалуйста отключи микрофон'))

    def test_metrics_percentiles_and_bounded_samples(self):
        metrics = LatencyMetrics(capacity=3)
        for value in (1, 2, 3, 4):
            metrics.record('vad', value)
        summary = metrics.summary()['vad']
        self.assertEqual(summary['count'], 3)
        self.assertEqual(summary['mean_ms'], 3000)
        self.assertEqual(summary['p50_ms'], 3000)

    def test_settings_reject_secrets_and_invalid_format(self):
        with self.assertRaises(ValueError):
            Settings.from_dict({'api_key': 'secret'})
        with self.assertRaises(ValueError):
            Settings.from_dict({'silence_ms': -1})
        self.assertNotIn('api_key', Settings().to_dict())


if __name__ == '__main__':
    unittest.main()
