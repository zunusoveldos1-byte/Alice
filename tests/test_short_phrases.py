from time import perf_counter,sleep
from unittest import TestCase,IsolatedAsyncioTestCase
from unittest.mock import Mock,patch
from threading import Event
from PyQt6.QtCore import QCoreApplication
from alice_voice.config import Settings
from alice_voice.workers import CaptureWorker,WorkerSignals
from alice_voice.metrics import LatencyMetrics
from alice_voice.controller import VoiceSessionController

APP=QCoreApplication.instance() or QCoreApplication([])


class ShortCaptureTests(TestCase):
    def capture(self,text,voiced=3,manual=False):
        base=perf_counter();signals=WorkerSignals();phrases=[];states=[];worker=None
        signals.phrase.connect(lambda epoch,p:phrases.append(p))
        signals.mic.connect(lambda epoch,s:states.append(s))
        class Capture:
            def __init__(self,inbox,device):self.inbox=inbox
            def open(self):
                for i in range(10):self.inbox.push((b'\xe8\x03' if i<voiced else b'\0\0')*512,base+(i+1)*.032)
            def close(self):pass
        class Vad:
            def __init__(self,path):self.i=0
            def __call__(self,pcm,rate):
                self.i+=1
                if manual and self.i==2:worker.hold.release(base+.064)
                return .95 if pcm[0] else 0
            def reset(self):pass
        class Stt:
            def __init__(self,path):pass
            def feed(self,pcm):return text
            def finalize(self):return text
            def reset(self):pass
        settings=Settings(fair_mode=manual,owner_only=manual,silence_ms=64)
        worker=CaptureWorker(settings,signals,LatencyMetrics(),0,Capture,Vad,Stt)
        if manual:worker.hold.press('physical',base,base)
        with patch('alice_voice.owner_voice.SpeakerEngine',side_effect=RuntimeError('No optional speaker model')) as engine:
            worker.start()
            try:
                deadline=perf_counter()+.4
                while perf_counter()<deadline:APP.processEvents();sleep(.005)
            finally:worker.stop();worker.join(3);APP.processEvents()
            engine.assert_not_called()
        return phrases,states

    def test_default_normal_mode_passes_short_finished_words_without_owner(self):
        self.assertFalse(Settings().owner_only)
        for text in ('Привет','Да','Нет','Спасибо'):
            with self.subTest(text=text):
                phrases,states=self.capture(text)
                self.assertEqual([p['text'] for p in phrases],[text])
                self.assertNotIn('error',states)

    def test_single_frame_click_is_still_discarded(self):
        phrases,states=self.capture('noise',voiced=1)
        self.assertEqual(phrases,[])

    def test_explicit_short_hold_works_without_optional_owner_model(self):
        for text in ('Привет','Да','Нет','Спасибо'):
            with self.subTest(text=text):
                phrases,states=self.capture(text,voiced=2,manual=True)
                held=[p for p in phrases if p.get('ptt')]
                self.assertEqual([p['text'] for p in held],[text])
                self.assertNotIn('error',states)

    def test_short_normal_word_reaches_runtime(self):
        for text in ('Привет','Да','Нет','Спасибо'):
            c=VoiceSessionController(Settings());c.timer.stop();c.mic_state='listening';c.runtime=Mock()
            try:
                now=perf_counter()
                c._phrase(0,dict(id=text,text=text,start=now,speech_end=now+.1,ready=now+.2))
                self.assertEqual(c.runtime.submit.call_args.args[1],text)
            finally:c.runtime=None;c.shutdown();c._poll()

    def test_old_owner_flag_does_not_gate_normal_conversation_or_show_profile_notice(self):
        c=VoiceSessionController(Settings(owner_only=True));c.timer.stop();c.mic_state='listening';c.runtime=Mock()
        notices=[];c.notification.connect(notices.append)
        try:
            now=perf_counter()
            c._phrase(0,dict(id='short',text='Привет',start=now,speech_end=now+.1,ready=now+.2))
            c.runtime.submit.assert_called_once()
            c._owner_notice(0)
            self.assertEqual(notices,[])
        finally:c.runtime=None;c.shutdown();c._poll()

    def test_unrecognized_normal_speech_shows_brief_retry_message(self):
        c=VoiceSessionController(Settings());c.timer.stop();c.mic_state='listening';c.runtime=Mock()
        notices=[];c.notification.connect(notices.append)
        try:
            now=perf_counter()
            c._phrase(0,dict(id='empty',text='',start=now,speech_end=now+.1,ready=now+.2))
            self.assertEqual(notices,['Не расслышала. Повторите'])
            c.runtime.submit.assert_not_called()
        finally:c.runtime=None;c.shutdown();c._poll()


class ShortRouterTests(IsolatedAsyncioTestCase):
    async def test_no_is_conversation_unless_a_control_is_pending(self):
        from alice_voice.context_tools import ToolRouter
        backend=Mock();backend.active_window.return_value={}
        router=ToolRouter(Settings(),desktop=backend)
        self.assertIsNone(await router.handle('Нет',Event(),turn_id='one'))
        router.power.request('shutdown','request')
        result=await router.handle('Нет',Event(),turn_id='two')
        self.assertEqual(result.status,'cancelled');self.assertFalse(router.power.pending)
        backend.execute.assert_not_called()
