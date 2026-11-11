"""Local final-pass STT. Bounded PCM jobs never block the capture thread."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import BoundedSemaphore, Lock
from time import perf_counter

import numpy as np

from .model_cache import LocalModelCache

_CACHE = LocalModelCache()
_INFERENCE_LOCK = Lock()


class WhisperRefiner:
    def __init__(self, model_path=None, *, native=None):
        if native is not None:
            self.native = native
            return
        try:
            path = Path(model_path).resolve()
            if not all((path / name).is_file() for name in ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt')):
                raise ValueError('missing local weights')
            from faster_whisper import WhisperModel
            self.native = _CACHE.get(path, lambda: WhisperModel(
                str(path), device='cpu', compute_type='int8', cpu_threads=4,
                num_workers=1, local_files_only=True))
        except Exception:
            raise RuntimeError('Не удалось загрузить локальный Whisper. Проверьте модель и requirements-whisper.txt.') from None

    def refine(self, pcm, *, cancelled=None):
        cancelled = cancelled or (lambda: False)
        if not pcm or len(pcm) % 2:
            return ''
        audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
        with _INFERENCE_LOCK:
            if cancelled(): return ''
            segments, _ = self.native.transcribe(
                audio, language='ru', beam_size=5, temperature=0,
                condition_on_previous_text=False, vad_filter=False, chunk_length=10)
            parts = []
            try:
                for segment in segments:
                    if cancelled(): return ''
                    if not (segment.no_speech_prob > .6 and segment.avg_logprob < -1.) and segment.text.strip():
                        parts.append(segment.text.strip())
            finally:
                close = getattr(segments, 'close', None)
                if close is not None: close()
            return ' '.join(parts).strip()


class RefinementQueue:
    def __init__(self, refiner, stop, valid, emit, failed, metrics=None):
        self.refiner, self.stop, self.valid = refiner, stop, valid
        self.emit, self.failed, self.metrics = emit, failed, metrics
        self.slots = BoundedSemaphore(2)  # One running phrase, one pending.
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='alice-whisper')

    def submit(self, phrase, pcm):
        if self.stop.is_set() or not self.slots.acquire(blocking=False):
            return False
        def work():
            try:
                if self.stop.is_set() or not self.valid(phrase):
                    return
                started = perf_counter()
                text = self.refiner.refine(pcm, cancelled=lambda: self.stop.is_set() or not self.valid(phrase))
                if self.metrics is not None:
                    self.metrics.record('whisper', perf_counter() - started)
                if not self.stop.is_set() and self.valid(phrase):
                    self.emit(dict(phrase, text=text))
            except Exception:
                if not self.stop.is_set() and self.valid(phrase):
                    self.failed('Не удалось распознать фразу Whisper. Повторите запрос.')
            finally:
                self.slots.release()
        try:
            self.executor.submit(work)
        except RuntimeError:
            self.slots.release()
            return False
        return True

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)
