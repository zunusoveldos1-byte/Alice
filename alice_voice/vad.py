"""Local Silero ONNX inference; each audio stream owns its recurrent state."""
from pathlib import Path

from .audio import FRAME_SAMPLES, RATE, validate_pcm_frame
from .model_cache import LocalModelCache

_MODEL_CACHE = LocalModelCache()


class SileroVAD:
    """Stateful Silero v5/v6 standard ONNX adapter, one instance per stream."""
    def __init__(self, model_path):
        import numpy as np
        import onnxruntime as ort
        self.np = np
        if not Path(model_path).is_file():
            raise RuntimeError('Не найдена локальная ONNX-модель Silero. Укажите путь в настройках.')
        def load():
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = opts.inter_op_num_threads = 1
            session = ort.InferenceSession(str(model_path), sess_options=opts, providers=['CPUExecutionProvider'])
            if {v.name for v in session.get_inputs()} != {'input', 'state', 'sr'}:
                raise ValueError('unsupported model')
            return session
        try:
            self.session = _MODEL_CACHE.get(model_path, load)
        except Exception:
            raise RuntimeError('Нужна стандартная Silero ONNX с входами input/state/sr (v5/v6).') from None
        self.reset()

    def reset(self):
        self.state = self.np.zeros((2, 1, 128), dtype=self.np.float32)
        self.context = self.np.zeros((1, 64), dtype=self.np.float32)

    def predict(self, pcm):
        """Compatibility entry point for one PCM16 mono frame."""
        validate_pcm_frame(pcm)
        return self(pcm, RATE)

    def __call__(self, audio_chunk, sample_rate=RATE):
        """Return speech probability for exactly 512 mono samples at 16 kHz.

        Accept little-endian PCM16 bytes or a one-dimensional NumPy int16 /
        normalized float32 array. Silero adds 64 context samples internally;
        callers must not prepend context or pad arbitrary chunk lengths.
        """
        np = self.np
        if sample_rate != RATE:
            raise ValueError('Silero VAD требует частоту 16000 Гц.')
        if isinstance(audio_chunk, bytes):
            validate_pcm_frame(audio_chunk)
            samples = np.frombuffer(audio_chunk, dtype='<i2')
        else:
            samples = np.asarray(audio_chunk)
        if samples.shape != (FRAME_SAMPLES,):
            raise ValueError('Silero VAD требует ровно 512 mono отсчётов.')
        if samples.dtype == np.dtype('int16'):
            samples = samples.astype(np.float32) / 32768.0
        elif samples.dtype != np.dtype('float32'):
            raise ValueError('Silero VAD принимает только int16 или float32.')
        if not np.isfinite(samples).all() or np.any(np.abs(samples) > 1):
            raise ValueError('Отсчёты float32 должны быть конечными и лежать в [-1, 1].')
        samples = samples.reshape(1, -1)
        extended = np.concatenate((self.context, samples), axis=1)
        output, self.state = self.session.run(None, {
            'input': extended, 'state': self.state, 'sr': np.array(RATE, dtype=np.int64),
        })
        self.context = samples[:, -64:].copy()
        return float(output[0][0])

