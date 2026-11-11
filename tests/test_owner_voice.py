from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest import TestCase
from unittest.mock import Mock
from unittest.mock import patch
from threading import Event
import asyncio
import numpy as np
from PyQt6.QtCore import QCoreApplication
from alice_voice.config import Settings
from alice_voice.controller import VoiceSessionController
from alice_voice.owner_voice import OwnerProfile, OwnerVerifier

APP = QCoreApplication.instance() or QCoreApplication([])


class OwnerTests(TestCase):
    def test_profile_requires_three_consistent_samples_and_valid_vectors(self):
        with TemporaryDirectory() as directory:
            profile=OwnerProfile(Path(directory)/'owner.json')
            with self.assertRaises(ValueError): profile.enroll([[1.,0.]],'abc')
            with self.assertRaises(ValueError): profile.enroll([[1.,0.],[0.,1.],[-1.,0.]],'abc')
            with self.assertRaises(ValueError): profile.enroll([[float('nan'),0.]]*3,'abc')
            self.assertFalse(profile.path.exists())
            profile.enroll([[1.,0.],[.99,.01],[.98,.02]],'abc')
            self.assertTrue(profile.matches([1.,0.],'abc',.65))
            self.assertFalse(profile.matches([0.,1.],'abc',.65))
            self.assertFalse(profile.matches([1.,0.],'different-model',.65))
            profile.path.write_text('{bad',encoding='utf-8')
            self.assertFalse(profile.matches([1.,0.],'abc',.65))

    def test_verifier_rejects_silence_short_audio_and_unknown_speaker(self):
        with TemporaryDirectory() as directory:
            profile=OwnerProfile(Path(directory)/'owner.json');profile.enroll([[1.,0.]]*3,'abc')
            engine=Mock();engine.fingerprint='abc';engine.embedding.return_value=np.array([1.,0.])
            verifier=OwnerVerifier(profile,engine,.65)
            self.assertFalse(verifier.verify(bytes(32000)))
            samples=(np.ones(32000)*1000).astype('<i2').tobytes()
            self.assertTrue(verifier.verify(samples))
            engine.embedding.return_value=np.array([0.,1.]);self.assertFalse(verifier.verify(samples))

    def test_foreign_prefix_cannot_be_authenticated_by_owner_suffix(self):
        with TemporaryDirectory() as directory:
            profile=OwnerProfile(Path(directory)/'owner.json');profile.enroll([[1.,0.]]*3,'abc')
            engine=Mock();engine.fingerprint='abc'
            def vector(pcm):
                samples=np.frombuffer(pcm,dtype='<i2')
                return [0.,1.] if np.any(samples < 0) else [1.,0.]
            engine.embedding.side_effect=vector
            audio=np.concatenate([np.full(16000*2,-1000),np.full(16000*7,1000)]).astype('<i2').tobytes()
            self.assertFalse(OwnerVerifier(profile,engine,.65).verify(audio))
            self.assertTrue(OwnerVerifier(profile,engine,.65).verify(np.full(16000*9,1000,dtype='<i2').tobytes()))

    def test_cancel_before_atomic_profile_commit_preserves_existing_profile(self):
        with TemporaryDirectory() as directory:
            profile=OwnerProfile(Path(directory)/'owner.json');profile.enroll([[1.,0.]]*3,'abc')
            before=profile.path.read_bytes();cancel=Event()
            with patch('alice_voice.owner_voice.os.fsync',side_effect=lambda fd:cancel.set()):
                with self.assertRaises(asyncio.CancelledError): profile.enroll([[0.,1.]]*3,'new',cancel)
            self.assertEqual(profile.path.read_bytes(),before)

    def test_unverified_phrase_cannot_submit_stop_or_interrupt(self):
        c=VoiceSessionController(Settings(owner_only=True,fair_mode=True));c.timer.stop();c.mic_state='listening';c.runtime=Mock()
        try:
            token=c.guard.begin('current');c.answer='Готовлю ответ'
            now=perf_counter()
            phrase={'id':'intruder','text':'стоп','start':now,'speech_end':now,'ready':now,
                    'owner_verified':False,'stable':True,'acoustic':'independent'}
            c._voice_evidence(0,phrase);c._phrase(0,phrase)
            self.assertEqual(c.guard.active,token);c.runtime.submit.assert_not_called()
        finally:
            c.runtime=None;c.shutdown();c._poll()

    def test_verified_owner_phrase_reaches_runtime(self):
        c=VoiceSessionController(Settings(owner_only=True,fair_mode=True));c.timer.stop();c.mic_state='listening';c.runtime=Mock()
        try:
            now=perf_counter()
            c._phrase(0,{'id':'owner','text':'Алиса, Как дела','start':now,'speech_end':now,'ready':now,'owner_verified':True,'confidence':.95})
            c.runtime.submit.assert_called_once()
        finally:
            c.runtime=None;c.shutdown();c._poll()

    def test_forget_command_survives_disfluency_filter(self):
        c=VoiceSessionController(Settings());c.timer.stop();c.mic_state='listening';c.runtime=Mock()
        try:
            now=perf_counter()
            c._phrase(0,{'id':'forget','text':'забудь факт один','start':now,'speech_end':now,'ready':now})
            self.assertEqual(c.runtime.submit.call_args.args[1], 'забудь факт один')
        finally:
            c.runtime=None;c.shutdown();c._poll()

    def test_cancelled_search_does_not_reach_runtime(self):
        c=VoiceSessionController(Settings());c.timer.stop();c.mic_state='listening';c.runtime=Mock()
        try:
            now=perf_counter()
            c._phrase(0,{'id':'cancel-search','text':'найди в интернете котов ой нет','start':now,'speech_end':now,'ready':now})
            c.runtime.submit.assert_not_called()
        finally:
            c.runtime=None;c.shutdown();c._poll()
