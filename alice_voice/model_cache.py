"""Bounded native-model reuse. Audio and per-stream state never enter this cache."""
from pathlib import Path
from threading import Lock


class LocalModelCache:
    """Retain the latest successful load, invalidated by path or file metadata."""
    def __init__(self):
        self._lock = Lock()
        self._key = None
        self._model = None

    @staticmethod
    def _signature(path):
        path = Path(path).resolve()
        stat = path.stat()  # Missing models must not silently reuse stale weights.
        if path.is_dir():
            files = []
            for child in sorted(path.rglob('*')):
                if child.is_file():
                    entry = child.stat()
                    files.append((str(child.relative_to(path)), entry.st_size, entry.st_mtime_ns))
            return path, stat.st_mtime_ns, tuple(files)
        return path, stat.st_size, stat.st_mtime_ns

    def get(self, path, loader):
        with self._lock:
            key = self._signature(path)
            if self._key == key:
                return self._model
            model = loader()  # Failure leaves no entry for the attempted load.
            self._key, self._model = key, model
            return model
