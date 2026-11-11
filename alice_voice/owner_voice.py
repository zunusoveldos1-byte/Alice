"""Local speaker filtering. Stores embeddings, never microphone recordings."""
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock

import numpy as np
from .memory import check_cancel


def unit(vector):
    value = np.asarray(vector, dtype=np.float32)
    if value.ndim != 1 or not 2 <= value.size <= 2048 or not np.isfinite(value).all():
        raise ValueError('Некорректный вектор голоса.')
    norm = float(np.linalg.norm(value))
    if norm < .00001:
        raise ValueError('Пустой вектор голоса.')
    return value / norm


class OwnerProfile:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = RLock()
        self._cached_stamp = None
        self._cached_profile = None

    def _stamp(self):
        stat = self.path.stat()
        return (str(self.path.absolute()), stat.st_dev, stat.st_ino, stat.st_size,
                stat.st_mtime_ns, stat.st_ctime_ns)

    def _invalidate(self):
        self._cached_stamp = self._cached_profile = None

    def enroll(self, embeddings, fingerprint, cancel=None):
        check_cancel(cancel)
        if len(embeddings) != 3:
            raise ValueError('Нужны три образца одного голоса.')
        vectors = [unit(value) for value in embeddings]
        if len({v.size for v in vectors}) != 1:
            raise ValueError('Размеры образцов не совпадают.')
        if min(float(a @ b) for a in vectors for b in vectors) < .60:
            raise ValueError('Образцы голоса не совпадают. Повторите в тихой комнате одним голосом.')
        center = unit(np.mean(vectors, axis=0))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent, suffix='.tmp', delete=False) as file:
                temporary = Path(file.name)
                json.dump({'version': 1, 'model': fingerprint, 'samples': 3, 'vector': center.tolist()}, file)
                file.flush()
                os.fsync(file.fileno())
            with self._lock:
                check_cancel(cancel)  # Commit boundary: cancellation preserves the old profile.
                os.replace(temporary, self.path)
                self._invalidate()
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def load(self):
        """Reuse normalized vectors, but check revocation/file changes on each call."""
        with self._lock:
            try:
                stamp = self._stamp()
                if stamp != self._cached_stamp:
                    self._invalidate()
                    if stamp[3] > 65536:
                        return None
                    data = json.loads(self.path.read_text(encoding='utf-8'))
                    if data.get('version') != 1 or data.get('samples') != 3 or not isinstance(data.get('model'), str):
                        return None
                    profile = data['model'], unit(data['vector'])
                    if self._stamp() != stamp:
                        return None  # Replacement/deletion during the read: retry next call.
                    self._cached_stamp, self._cached_profile = stamp, profile
                fingerprint, vector = self._cached_profile
                return fingerprint, vector.copy()  # Never expose the cached identity for mutation.
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                self._invalidate()
                return None

    def matches(self, embedding, fingerprint, threshold):
        profile = self.load()
        if profile is None or profile[0] != fingerprint:
            return False
        try:
            vector = unit(embedding)
            return vector.shape == profile[1].shape and float(vector @ profile[1]) >= threshold
        except ValueError:
            return False


class SpeakerEngine:
    def __init__(self, model_path):
        path = Path(model_path)
        if not path.is_file():
            raise RuntimeError('Не найдена модель проверки владельца. Подготовьте её по README.')
        try:
            import sherpa_onnx
        except ImportError:
            raise RuntimeError('Установите зависимости requirements-speaker.txt для проверки владельца.') from None
        digest = hashlib.sha256()
        with path.open('rb') as file:
            while chunk := file.read(1024 * 1024):
                digest.update(chunk)
        self.fingerprint = digest.hexdigest()
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(path.resolve()), num_threads=2, provider='cpu')
        if not config.validate():
            raise RuntimeError('Неверная модель проверки владельца.')
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)

    def embedding(self, pcm):
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
        if samples.size < 16000 or samples.size > 16000 * 30 or np.sqrt(np.mean(samples ** 2)) < .002:
            raise ValueError('Скажите фразу длительностью хотя бы одну секунду, ближе к микрофону.')
        stream = self.extractor.create_stream()
        stream.accept_waveform(sample_rate=16000, waveform=np.ascontiguousarray(samples))
        stream.input_finished()
        if not self.extractor.is_ready(stream):
            raise ValueError('Недостаточно речи для проверки владельца.')
        return unit(self.extractor.compute(stream))


class OwnerVerifier:
    def __init__(self, profile, engine, threshold):
        self.profile, self.engine, self.threshold = profile, engine, threshold

    def verify(self, pcm):
        if len(pcm) < 32000 or len(pcm) % 2:
            return False
        signal = np.frombuffer(pcm, dtype='<i2').astype(np.float32)
        if float(np.sqrt(np.mean(signal ** 2))) < 65:
            return False
        if len(signal) > 16000 * 121:
            return False
        try:
            # Cover the whole utterance, including its beginning. An owner's
            # suffix must never authorize another speaker's command prefix.
            count = len(signal)
            starts = list(range(0, max(1, count - 32000 + 1), 16000))
            if count > 32000 and starts[-1] != count - 32000:
                starts.append(count - 32000)
            for start in starts:
                window = pcm[start * 2:min(count, start + 32000) * 2]
                if not self.profile.matches(self.engine.embedding(window), self.engine.fingerprint, self.threshold):
                    return False
            return True
        except (ValueError, RuntimeError):
            return False


class LazyOwnerVerifier:
    """Load optional speaker weights only for automatic fair-mode requests."""
    def __init__(self,settings):
        self.settings=settings
        self.verifier=None

    def verify(self,pcm):
        if self.verifier is None:
            try:
                profile=OwnerProfile(Path(self.settings.data_directory)/'owner.json')
                engine=SpeakerEngine(self.settings.speaker_model)
                loaded=profile.load()
                if loaded is None or loaded[0]!=engine.fingerprint:return False
                self.verifier=OwnerVerifier(profile,engine,self.settings.speaker_threshold)
            except (RuntimeError,ValueError,OSError):
                return False
        return self.verifier.verify(pcm)


class VerificationQueue:
    """One in-flight verification; capture keeps consuming PCM independently."""
    def __init__(self, verifier, stop, emit, rejected=None):
        from concurrent.futures import ThreadPoolExecutor
        from threading import BoundedSemaphore
        self.verifier, self.stop, self.emit = verifier, stop, emit
        self.rejected = rejected
        self.last_rejected = 0.
        self.slot = BoundedSemaphore(1)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='alice-owner')

    def submit(self, phrase, pcm):
        if self.stop.is_set() or not self.slot.acquire(blocking=False):
            return False
        def work():
            try:
                if self.stop.is_set():
                    return
                accepted = self.verifier.verify(pcm)
                if not self.stop.is_set():
                    if accepted:
                        self.emit(dict(phrase, owner_verified=True))
                    elif self.rejected is not None:
                        from time import monotonic
                        now = monotonic()
                        if now - self.last_rejected >= 5:
                            self.last_rejected = now
                            self.rejected()
            finally:
                self.slot.release()
        try:
            self.executor.submit(work)
        except BaseException:
            self.slot.release()
            raise
        return True

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)
