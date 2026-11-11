"""Profile caching must preserve prompt revocation and caller isolation."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from threading import Event
import asyncio
import numpy as np

from alice_voice.owner_voice import OwnerProfile, OwnerVerifier


class ProfileCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'owner.json'
        self.profile=OwnerProfile(self.path)
        self.profile.enroll([[1.,0.]]*3,'model')

    def test_unchanged_profile_reads_json_once_across_audio_windows(self):
        original=Path.read_text
        reads=[]
        def read(path,*args,**kwargs):
            reads.append(path)
            return original(path,*args,**kwargs)
        engine=Mock(fingerprint='model')
        engine.embedding.return_value=np.array([1.,0.])
        pcm=np.full(16000*10,1000,dtype='<i2').tobytes()
        with patch.object(Path,'read_text',read):
            self.assertTrue(OwnerVerifier(self.profile,engine,.65).verify(pcm))
        self.assertGreater(engine.embedding.call_count,1)
        self.assertEqual(reads,[self.path])

    def test_loaded_vectors_cannot_mutate_cached_identity(self):
        fingerprint,vector=self.profile.load()
        vector[:]=[0.,1.]
        self.assertTrue(self.profile.matches([1.,0.],fingerprint,.65))
        self.assertFalse(self.profile.matches([0.,1.],fingerprint,.65))

    def test_other_instance_enrollment_replaces_cached_identity(self):
        self.assertTrue(self.profile.matches([1.,0.],'model',.65))
        OwnerProfile(self.path).enroll([[0.,1.]]*3,'model')
        self.assertFalse(self.profile.matches([1.,0.],'model',.65))
        self.assertTrue(self.profile.matches([0.,1.],'model',.65))

    def test_deleted_or_corrupted_profile_never_uses_cached_identity(self):
        self.assertTrue(self.profile.matches([1.,0.],'model',.65))
        self.path.unlink()
        self.assertFalse(self.profile.matches([1.,0.],'model',.65))
        self.profile.enroll([[1.,0.]]*3,'model')
        self.assertTrue(self.profile.matches([1.,0.],'model',.65))
        self.path.write_text('{bad',encoding='utf-8')
        self.assertFalse(self.profile.matches([1.,0.],'model',.65))

    def test_atomic_replacement_with_same_size_and_mtime_is_detected(self):
        self.assertTrue(self.profile.matches([1.,0.],'model',.65))
        stamp=self.path.stat()
        other=self.path.with_name('replacement.json')
        other.write_bytes(self.path.read_bytes().replace(b'[1.0, 0.0]',b'[0.0, 1.0]'))
        os.utime(other,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
        os.replace(other,self.path)
        self.assertFalse(self.profile.matches([1.,0.],'model',.65))

    def test_profile_deleted_during_read_is_not_accepted(self):
        original=Path.read_text
        def deleted(path,*args,**kwargs):
            content=original(path,*args,**kwargs)
            path.unlink()
            return content
        with patch.object(Path,'read_text',deleted):
            self.assertIsNone(self.profile.load())

    def test_parallel_loads_read_one_snapshot(self):
        original=Path.read_text
        reads=[]
        def read(path,*args,**kwargs):
            reads.append(path)
            return original(path,*args,**kwargs)
        with patch.object(Path,'read_text',read), ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(lambda _:self.profile.load(),range(4)))
        self.assertTrue(all(row is not None for row in results))
        self.assertEqual(reads,[self.path])

    def test_permission_failure_purges_cached_value(self):
        self.assertIsNotNone(self.profile.load())
        with patch.object(Path,'stat',side_effect=PermissionError('denied')):
            self.assertIsNone(self.profile.load())
        original=Path.read_text
        reads=[]
        def read(path,*args,**kwargs):
            reads.append(path)
            return original(path,*args,**kwargs)
        with patch.object(Path,'read_text',read):
            self.assertTrue(self.profile.matches([1.,0.],'model',.65))
        self.assertEqual(reads,[self.path])

    def test_cancelled_enrollment_preserves_cached_identity(self):
        self.assertTrue(self.profile.matches([1.,0.],'model',.65))
        cancel=Event()
        with patch('alice_voice.owner_voice.os.fsync',side_effect=lambda fd:cancel.set()):
            with self.assertRaises(asyncio.CancelledError):
                self.profile.enroll([[0.,1.]]*3,'model',cancel)
        self.assertTrue(self.profile.matches([1.,0.],'model',.65))
        self.assertFalse(self.profile.matches([0.,1.],'model',.65))
