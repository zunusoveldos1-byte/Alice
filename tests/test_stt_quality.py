"""Audio format, speech confirmation and raw STT diagnostic regressions."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from alice_voice.audio import AudioCapture, AudioFrame, FrameInbox
from alice_voice.recognition import PhraseSegmenter, VoskRecognizer


def frame(index: int, value: int = 1000) -> AudioFrame:
    return AudioFrame(value.to_bytes(2, 'little', signed=True) * 512, (index + 1) * .032)


class AudioQualityTests(unittest.TestCase):
    def test_damage_diagnostics_distinguish_queue_device_and_format(self):
        import sounddevice as sd
        inbox = FrameInbox(capacity=1)
        capture = AudioCapture(inbox)
        capture.callback(bytes(1024), 512, None, None)
        capture.callback(bytes(1024), 512, None, None)
        status = sd.CallbackFlags()
        status.input_overflow = True
        capture.callback(bytes(1024), 512, None, status)
        capture.callback(b'bad', 512, None, None)
        self.assertEqual(inbox.damage_counts, {
            'queue_full': 1, 'input_overflow': 1, 'invalid_frame': 1})
        inbox.clear()
        self.assertFalse(inbox.damaged.is_set())
        self.assertEqual(inbox.damage_counts['queue_full'], 1)
        snapshot = inbox.damage_counts
        snapshot.clear()
        self.assertEqual(inbox.damage_counts['input_overflow'], 1)

    def test_invalid_callback_frame_is_never_queued(self):
        for pcm, count, status in ((b'bad', 512, None), (bytes(1024), 511, None),
                                   (bytes(1024), 512, True)):
            with self.subTest(count=count, status=status):
                inbox = FrameInbox()
                AudioCapture(inbox).callback(pcm, count, None, status)
                self.assertTrue(inbox.damaged.is_set())
                self.assertEqual(inbox.size, 0)

    def test_capture_requests_exact_format_and_checks_actual_stream(self):
        stream = Mock(samplerate=48000, channels=2, dtype='float32')
        sd = SimpleNamespace(check_input_settings=Mock(), RawInputStream=Mock(return_value=stream))
        with patch.dict('sys.modules', {'sounddevice': sd}):
            with self.assertRaises(RuntimeError):
                AudioCapture(FrameInbox(), 7).open()
        kwargs = sd.RawInputStream.call_args.kwargs
        self.assertEqual((kwargs['samplerate'], kwargs['channels'], kwargs['dtype']), (16000, 1, 'int16'))
        stream.start.assert_not_called()
        stream.close.assert_called_once()


class SpeechConfirmationTests(unittest.TestCase):
    def test_click_and_long_silence_never_reach_stt(self):
        segmenter = PhraseSegmenter(silence_ms=650)
        updates = [segmenter.feed(frame(i), .9 if i == 0 else 0) for i in range(23)]
        self.assertFalse(any(update.frames or update.started or update.finished for update in updates))
        self.assertTrue(any(update.discarded for update in updates))

    def test_confirmation_keeps_original_onset_and_preroll(self):
        segmenter = PhraseSegmenter(preroll_ms=64,min_speech_ms=320)
        for i in range(2):
            segmenter.feed(frame(i), 0)
        for i in range(2, 11):
            update = segmenter.feed(frame(i), .9)
            self.assertEqual(update.frames, [])
        update = segmenter.feed(frame(11), .9)
        self.assertTrue(update.started)
        self.assertEqual(len(update.frames), 12)
        self.assertEqual(update.frames[0], frame(0))
        self.assertEqual(update.frames[-1], frame(11))
        self.assertAlmostEqual(update.phrase_start, .064)
        self.assertAlmostEqual(update.audio_start, 0)

    def test_preroll_and_silence_do_not_count_as_voiced_duration(self):
        segmenter = PhraseSegmenter(preroll_ms=320, silence_ms=64,min_speech_ms=320)
        for i in range(10):
            segmenter.feed(frame(i), 0)
        for i in range(10, 19):
            self.assertEqual(segmenter.feed(frame(i), .9).frames, [])
        segmenter.feed(frame(19), 0)
        update = segmenter.feed(frame(20), 0)
        self.assertTrue(update.discarded)
        self.assertFalse(update.finished)
        self.assertEqual(update.frames, [])

    def test_short_candidate_cannot_contaminate_next_phrase(self):
        segmenter = PhraseSegmenter(preroll_ms=0, silence_ms=64,min_speech_ms=320)
        for i, probability in enumerate((.9, 0, 0)):
            segmenter.feed(frame(i), probability)
        for i in range(3, 13):
            update = segmenter.feed(frame(i), .9)
        self.assertTrue(update.started)
        self.assertEqual(update.frames[0], frame(3))
        self.assertEqual(len(update.frames), 10)


class RecognizerDiagnosticsTests(unittest.TestCase):
    def native(self):
        return Mock(AcceptWaveform=Mock(return_value=False),
                    PartialResult=Mock(return_value=json.dumps({'partial': 'привет'})),
                    FinalResult=Mock(return_value=json.dumps({'text': 'привет'})))

    def test_partial_and_final_logs_include_lengths_and_rms_without_transcript(self):
        native = self.native()
        recognizer = VoskRecognizer(native=native)
        pcm = (16384).to_bytes(2, 'little', signed=True) * 512
        with self.assertLogs('alice_voice.recognition', level='INFO') as logs:
            recognizer.feed(pcm)
            recognizer.finalize()
        output = '\n'.join(logs.output)
        self.assertIn('stt_partial', output)
        self.assertIn('stt_final', output)
        self.assertNotIn('привет', output)
        self.assertIn('chars=', output)
        self.assertIn('rms=0.500000', output)
        native.Reset.assert_called()

    def test_bad_pcm_never_reaches_native_vosk(self):
        native = self.native()
        with self.assertRaises(ValueError):
            VoskRecognizer(native=native).feed(b'bad')
        native.AcceptWaveform.assert_not_called()

    def test_rms_uses_signed_int16_without_overflow(self):
        from alice_voice.audio import pcm_rms
        self.assertEqual(pcm_rms(bytes(1024)), 0)
        self.assertEqual(pcm_rms((-32768).to_bytes(2, 'little', signed=True) * 512), 1)

    def test_device_list_excludes_outputs_and_labels_host_api(self):
        from alice_voice.audio import list_input_devices
        sd = SimpleNamespace(
            query_devices=lambda: [dict(name='Speakers', max_input_channels=0),
                                   dict(name='Realtek Mic', max_input_channels=2, hostapi=0)],
            query_hostapis=lambda: [dict(name='WASAPI')],
            default=SimpleNamespace(device=(1, 0)), check_input_settings=Mock())
        with patch.dict('sys.modules', {'sounddevice': sd}):
            devices = list_input_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].index, 1)
        self.assertIn('Realtek Mic', devices[0].label)
        self.assertIn('WASAPI', devices[0].label)
        self.assertTrue(devices[0].is_default)


if __name__ == '__main__':
    unittest.main()
