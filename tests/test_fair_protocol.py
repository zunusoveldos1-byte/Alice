from time import perf_counter
import unittest
from unittest.mock import Mock
from PyQt6.QtCore import QCoreApplication
from alice_voice.config import Settings
from alice_voice.controller import VoiceSessionController


APP=QCoreApplication.instance() or QCoreApplication([])


class FairProtocolTests(unittest.TestCase):
    def setUp(self):
        self.c=VoiceSessionController(Settings(fair_mode=True,owner_only=True))
        self.c.timer.stop();self.c.mic_state='listening';self.c.runtime=Mock()
        self.requests=[]
        self.c.fair_protocol_requested.connect(self.requests.append)

    def tearDown(self):
        self.c.runtime=None;self.c.shutdown();self.c._poll()

    def phrase(self,text,**changes):
        now=perf_counter()
        return dict(id=text,text=text,start=now,speech_end=now+.5,ready=now+.7,
                    owner_verified=changes.get('owner_verified',True),confidence=.95)

    def test_protocol_commands_bypass_wake_gate_but_do_not_reach_llm(self):
        self.c._phrase(0,self.phrase('Протокол Ярмарка'))
        self.c._phrase(0,self.phrase('Ярмарка стоп'))
        self.assertEqual(self.requests,[True,False])
        self.c.runtime.submit.assert_not_called()

    def test_stop_fair_in_natural_word_order_exits_without_llm(self):
        self.c._phrase(0,self.phrase('Стоп ярмарка'))
        self.assertEqual(self.requests,[False])
        self.c.runtime.submit.assert_not_called()

    def test_unverified_voice_and_own_playback_cannot_change_mode(self):
        self.c._phrase(0,self.phrase('Ярмарка стоп',owner_verified=False))
        self.c.echo.start(perf_counter()-.1)
        self.c._phrase(0,self.phrase('Протокол Ярмарка'))
        self.assertEqual(self.requests,[])

    def test_exact_commands_only(self):
        from alice_voice.fair_mode import protocol_command
        for text,result in [('Протокол Ярмарка',True),('Ярмарка стоп!',False),
                            ('Стоп ярмарка',False),('Выключи ярмарку',False),
                            ('Отключи протокол ярмарка',False),('Алиса, протокол ярмарка',True)]:
            self.assertIs(protocol_command(text,'алиса'),result)
        for text in ('не включай протокол ярмарка','что такое протокол ярмарка','ярмарка стоп выключи компьютер'):
            self.assertIsNone(protocol_command(text,'алиса'))


class FairProtocolUI(unittest.TestCase):
    def test_visible_badge_exits_persisted_fair_mode_without_voice(self):
        from tests.test_main_window import MainWindowTests
        MainWindowTests.run_ui(self,'''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from time import perf_counter
            from PyQt6.QtWidgets import QApplication, QPushButton
            from PyQt6.QtTest import QTest
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app=QApplication([])
            with TemporaryDirectory() as directory:
                path=Path(directory)/'settings.json'
                c=VoiceSessionController(Settings(fair_mode=True,noise_mode='normal'))
                w=MainWindow(c,path)
                assert isinstance(w.fair_badge,QPushButton)
                w.fair_badge.click()
                deadline=perf_counter()+3
                while (w.settings_store.busy or c.reconfiguring) and perf_counter()<deadline:QTest.qWait(20)
                assert not c.settings.fair_mode
                assert c.settings.noise_mode=='normal'
                assert not Settings.load(path).fair_mode
                w.allow_close=True;w.close();c.shutdown()
        ''')

    def test_commands_persist_mode_and_repeat_start_preserves_previous_noise(self):
        from tests.test_main_window import MainWindowTests
        MainWindowTests.run_ui(self,'''
            import json
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from time import perf_counter
            from PyQt6.QtWidgets import QApplication
            from PyQt6.QtTest import QTest
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app=QApplication([])
            with TemporaryDirectory() as directory:
                path=Path(directory)/'settings.json'
                c=VoiceSessionController(Settings(noise_mode='normal'))
                w=MainWindow(c,path)
                def wait():
                    deadline=perf_counter()+3
                    while (w.settings_store.busy or c.reconfiguring) and perf_counter()<deadline:QTest.qWait(20)
                    assert not w.settings_store.busy and not c.reconfiguring
                w._fair_protocol(True);wait()
                assert c.settings.fair_mode and c.settings.noise_mode=='fair'
                saved=json.loads(path.read_text(encoding='utf-8'))
                assert saved['fair_mode'] and saved['noise_mode']=='fair'
                w._fair_protocol(True);wait()
                w._fair_protocol(False);wait()
                assert not c.settings.fair_mode and c.settings.noise_mode=='normal'
                assert c.runtime is None and c.capture is None
                w.allow_close=True;w.close();c.shutdown()
        ''')
