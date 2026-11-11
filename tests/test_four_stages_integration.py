import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import perf_counter, sleep
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import QCoreApplication
from alice_voice.config import Settings
from alice_voice.audio import FrameInbox
from alice_voice.workers import CaptureWorker, DialogRuntime, WorkerSignals
from alice_voice.metrics import LatencyMetrics
from alice_voice.speech import PCM
import numpy as np

APP = QCoreApplication.instance() or QCoreApplication([])


class FullPhraseVerification(unittest.TestCase):
    def test_capture_verifies_foreign_prefix_not_only_owner_suffix(self):
        # >6 seconds of owner speech must not hide a foreign prefix.
        for foreign in (True, False):
            signals=WorkerSignals();emitted=[];verified=[];done=Event()
            signals.phrase.connect(lambda epoch,phrase:emitted.append(phrase))
            frames=([np.full(512,-1000 if foreign else 1000,dtype='<i2').tobytes()]*64 +
                    [np.full(512,1000,dtype='<i2').tobytes()]*224 + [bytes(1024)]*4)
            class Capture:
                def __init__(self,inbox,device): self.inbox=inbox
                def open(self):
                    for i,frame in enumerate(frames): self.inbox.push(frame,perf_counter()+i*.032)
                def close(self): pass
            class VAD:
                def __init__(self,path): pass
                def __call__(self,pcm,rate): return .9 if pcm != bytes(1024) else 0.
                def reset(self): pass
            class STT:
                def __init__(self,path): pass
                def reset(self): pass
                def feed(self,pcm): return 'алиса открой блокнот'
                def finalize(self): return 'алиса открой блокнот'
            engine=Mock(fingerprint='abc');verifier=Mock()
            def verify(pcm):
                verified.append(pcm);done.set()
                return not np.any(np.frombuffer(pcm,dtype='<i2') < 0)
            verifier.verify.side_effect=verify
            with patch('alice_voice.owner_voice.SpeakerEngine',return_value=engine), \
                 patch('alice_voice.owner_voice.OwnerProfile.load',return_value=('abc',np.array([1.,0.]))), \
                 patch('alice_voice.owner_voice.OwnerVerifier',return_value=verifier):
                worker=CaptureWorker(Settings(owner_only=True,fair_mode=True,silence_ms=64),signals,LatencyMetrics(),1,Capture,VAD,STT)
                worker.inbox=FrameInbox(400);worker.start()
                try:
                    self.assertTrue(done.wait(3))
                    if not foreign:
                        deadline=perf_counter()+2
                        while not emitted and perf_counter()<deadline:
                            APP.processEvents();sleep(.005)
                finally:
                    worker.stop();worker.join(3);APP.processEvents()
                self.assertFalse(worker.is_alive())
                self.assertTrue(verified)
                self.assertEqual(bool(np.any(np.frombuffer(verified[0],dtype='<i2')<0)),foreign)
                self.assertEqual(len(emitted),0 if foreign else 1)


class RuntimeActions(unittest.IsolatedAsyncioTestCase):
    async def test_local_command_is_spoken_without_calling_model(self):
        with TemporaryDirectory() as directory:
            r=DialogRuntime(Settings(data_directory=directory),WorkerSignals(),LatencyMetrics())
            r.provider=Mock();spoken=[]
            class Synth:
                async def synthesize(self,text,cancel): spoken.append(text);return PCM(bytes(3200),16000)
            r.synth=Synth()
            r.signals.pcm.connect(lambda token,pcm,when,seq:r._release_pcm(token,seq))
            await asyncio.wait_for(r._process(1,'Запомни: мой любимый язык Python',Event()),2)
            r.provider.stream.assert_not_called()
            self.assertTrue(spoken)
            self.assertIn('Запомнила',' '.join(spoken))
            self.assertTrue((Path(directory)/'memory.sqlite3').is_file())


class NewSettingsUI(unittest.TestCase):
    def test_new_settings_and_enrollment_cancel_are_accessible(self):
        from test_main_window import MainWindowTests
        MainWindowTests.run_ui(self, '''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from unittest.mock import patch
            from PyQt6.QtWidgets import QApplication
            from PyQt6.QtTest import QTest
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            from alice_voice.ui.owner_dialog import OwnerEnrollmentDialog
            app=QApplication([])
            with TemporaryDirectory() as directory, patch('alice_voice.ui.dialogs.MicrophoneDiscovery.start'):
                c=VoiceSessionController(Settings())
                w=MainWindow(c,Path(directory)/'settings.json');w.show();w._show_settings()
                page=w.settings_page
                for key in ('desktop_commands','memory_enabled','owner_only','firefox_path','speaker_threshold'):
                    assert key in page.inputs
                assert page.values()==c.settings
                dialog=OwnerEnrollmentDialog(c.settings,w)
                dialog.show();QTest.qWait(20);dialog.reject()
                assert not dialog.isVisible() and dialog.worker is None
                w.close();QTest.qWait(150)
        ''')
