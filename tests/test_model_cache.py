"""Native model cache contracts, with small local files and no inference."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import Mock

from alice_voice.model_cache import LocalModelCache


class ModelCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'model'
        self.path.write_bytes(b'weights')
        self.cache = LocalModelCache()

    def test_resolved_paths_reuse_one_model(self):
        loader = Mock(side_effect=object)
        first = self.cache.get(self.path, loader)
        self.assertIs(self.cache.get(self.path.parent / '.' / self.path.name, loader), first)
        loader.assert_called_once()

    def test_changed_file_reloads_and_missing_file_does_not_use_stale_model(self):
        loader = Mock(side_effect=object)
        first = self.cache.get(self.path, loader)
        self.path.write_bytes(b'new weights')
        self.assertIsNot(self.cache.get(self.path, loader), first)
        self.path.unlink()
        with self.assertRaises(OSError):
            self.cache.get(self.path, loader)
        self.assertEqual(loader.call_count, 2)

    def test_nested_model_file_changes_and_removals_invalidate_directory(self):
        self.path.unlink()
        nested = self.path / 'am' / 'final.mdl'
        nested.parent.mkdir(parents=True)
        nested.write_bytes(b'weights')
        loader = Mock(side_effect=object)
        first = self.cache.get(self.path, loader)
        nested.write_bytes(b'replaced weights')
        second = self.cache.get(self.path, loader)
        self.assertIsNot(first, second)
        nested.unlink()
        self.assertIsNot(self.cache.get(self.path, loader), second)

    def test_failure_can_retry_and_only_latest_model_is_retained(self):
        loader = Mock(side_effect=[RuntimeError('load failed'), object(), object(), object()])
        with self.assertRaises(RuntimeError):
            self.cache.get(self.path, loader)
        first = self.cache.get(self.path, loader)
        other = self.path.with_name('other')
        other.write_bytes(b'weights')
        self.cache.get(other, loader)
        self.assertIsNot(self.cache.get(self.path, loader), first)
        self.assertEqual(loader.call_count, 4)

    def test_parallel_requests_publish_one_initialization(self):
        entered, release = Event(), Event()
        def load():
            entered.set()
            if not release.wait(3): raise RuntimeError('test did not release loader')
            return object()
        loader = Mock(side_effect=load)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.cache.get, self.path, loader)
            try:
                self.assertTrue(entered.wait(2))
                second = executor.submit(self.cache.get, self.path, loader)
            finally:
                release.set()
            self.assertIs(first.result(2), second.result(2))
        loader.assert_called_once()
