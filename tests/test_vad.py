"""Exercise the local Silero model without a microphone or network access."""
from pathlib import Path
import unittest

import numpy as np

from alice_voice.recognition import SileroVAD

MODEL_PATH = Path(__file__).resolve().parents[1] / 'models/silero_vad.onnx'


@unittest.skipUnless(MODEL_PATH.is_file(), 'Local Silero ONNX model is not installed')
class LocalSileroTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vad = SileroVAD(MODEL_PATH)

    def setUp(self):
        self.vad.reset()

    def test_local_model_filters_silence(self):
        probabilities = [self.vad(np.zeros(512, dtype=np.float32), 16000) for _ in range(30)]
        self.assertTrue(all(0 <= probability < .5 for probability in probabilities))

    def test_int16_and_float32_produce_same_probability_and_context(self):
        pcm = np.tile(np.array([-32768, -16384, 0, 16384, 32767], dtype=np.int16), 103)[:512]
        integer_probability = self.vad(pcm, 16000)
        integer_state = self.vad.state.copy()
        self.vad.reset()
        float_probability = self.vad(pcm.astype(np.float32) / 32768, 16000)
        self.assertAlmostEqual(integer_probability, float_probability, places=7)
        np.testing.assert_allclose(self.vad.state, integer_state)
        np.testing.assert_array_equal(self.vad.context[0], pcm[-64:].astype(np.float32) / 32768)

    def test_invalid_chunks_do_not_change_stream_state(self):
        invalid = [
            (np.zeros(1532, dtype=np.float32), 16000),
            (np.zeros(511, dtype=np.float32), 16000),
            (np.zeros((2, 512), dtype=np.float32), 16000),
            (np.zeros(512, dtype=np.float32), 8000),
            (np.zeros(512, dtype=np.float64), 16000),
            (np.full(512, np.nan, dtype=np.float32), 16000),
            (np.full(512, np.inf, dtype=np.float32), 16000),
            (np.full(512, 1.1, dtype=np.float32), 16000),
        ]
        for chunk, rate in invalid:
            with self.subTest(shape=chunk.shape, dtype=chunk.dtype, rate=rate):
                with self.assertRaises(ValueError):
                    self.vad(chunk, rate)
                self.assertFalse(self.vad.state.any())
                self.assertFalse(self.vad.context.any())

    def test_pcm_predict_matches_callable_and_reset_is_reproducible(self):
        pcm = np.full(512, 8000, dtype='<i2')
        probability = self.vad.predict(pcm.tobytes())
        self.vad.reset()
        self.assertEqual(probability, self.vad(pcm, 16000))
        self.vad.reset()
        self.assertEqual(probability, self.vad.predict(pcm.tobytes()))
