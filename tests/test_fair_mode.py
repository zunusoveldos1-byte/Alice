import unittest
from types import SimpleNamespace
import numpy as np
from alice_voice.audio import AudioFrame


class FairLocalReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_noisy_feedback_uses_tts_queue_without_llm_or_tool_execution(self):
        from unittest.mock import Mock,AsyncMock
        from threading import Event
        import asyncio
        from PyQt6.QtCore import QCoreApplication
        from alice_voice.workers import DialogRuntime,WorkerSignals
        from alice_voice.config import Settings
        from alice_voice.metrics import LatencyMetrics
        from alice_voice.fair_mode import LocalReply
        from alice_voice.speech import PCM
        app=QCoreApplication.instance() or QCoreApplication([])
        runtime=DialogRuntime(Settings(),WorkerSignals(),LatencyMetrics())
        runtime.provider=Mock()
        runtime.router=SimpleNamespace(legacy=None,handle=AsyncMock(side_effect=AssertionError('Tools must not run')))
        class Synth:
            async def synthesize(self,text,cancel):return PCM(bytes(1024),16000)
        runtime.synth=Synth()
        packets=[]
        def packet(token,pcm,ready,sequence):
            packets.append(pcm)
            runtime._release_pcm(token,sequence)
        runtime.signals.pcm.connect(packet)
        await asyncio.wait_for(runtime._process(1,LocalReply('Здесь шумно. Повторите команду.'),Event()),2)
        self.assertTrue(packets)
        runtime.provider.stream.assert_not_called();runtime.router.handle.assert_not_called()
        self.assertEqual(list(runtime.history),[])


class FairTests(unittest.TestCase):
    def settings(self, **changes):
        return SimpleNamespace(fair_mode=True,noise_mode='off',wake_enabled=True,wake_name='алиса',
                               wake_window_s=5.,fair_continue=False,**changes)

    def test_wake_opens_one_bounded_command_and_never_accepts_bystander(self):
        from alice_voice.fair_mode import ActivationGate
        g=ActivationGate(self.settings())
        self.assertIsNone(g.accept('выключи компьютер',1,1))
        self.assertEqual(g.accept('алиса',2,2),'')
        self.assertEqual(g.accept('открой блокнот',3,3),'открой блокнот')
        self.assertIsNone(g.accept('закрой окно',4,4))
        self.assertEqual(g.accept('алиса',5,5),'')
        self.assertIsNone(g.accept('открой блокнот',11,11))

    def test_name_must_be_exact_prefix_and_inline_name_is_removed(self):
        from alice_voice.fair_mode import ActivationGate
        g=ActivationGate(self.settings())
        self.assertIsNone(g.accept('мне сказала алиса закрой окно',1,1))
        self.assertIsNone(g.accept('лиса открой блокнот',2,2))
        self.assertEqual(g.accept('Алиса, открой блокнот',3,3),'открой блокнот')

    def test_hold_has_preroll_and_finishes_once_after_release(self):
        from alice_voice.fair_mode import HoldToTalk
        h=HoldToTalk(preroll_ms=320,max_seconds=10)
        for i in range(10):h.feed(AudioFrame(bytes(1024),i*.032),0)
        h.press('token',.32,0)
        start=h.feed(AudioFrame(bytes(1024),.352),1)
        self.assertEqual(start['kind'],'start')
        self.assertGreater(len(start['frames']),1)
        h.release(.36)
        end=None
        for i in range(12,17):
            item=h.feed(AudioFrame(bytes(1024),i*.032),1)
            if item and item['kind']=='finish':end=item
        self.assertIsNotNone(end)
        self.assertEqual(end['token'],'token')
        self.assertIsNone(h.feed(AudioFrame(bytes(1024),.6),0))

    def test_rnnoise_reblocking_preserves_time_and_bounded_buffers(self):
        from alice_voice.noise import MicrophoneProcessor
        native=[]
        def identity(x):native.append(len(x));return x
        p=MicrophoneProcessor('normal',process_native=identity)
        frames=[]
        for i in range(150):
            frames.extend(p.process(AudioFrame(np.zeros(512,dtype='<i2').tobytes(),(i+1)*.032)))
        self.assertTrue(frames)
        self.assertEqual(set(native),{480})
        self.assertTrue(all(len(f.pcm)==1024 for f in frames))
        self.assertTrue(all(abs(b.captured_at-a.captured_at-.032)<1e-8 for a,b in zip(frames,frames[1:])))
        self.assertLess(p.pending_samples,4096)
        self.assertLess(150-len(frames),10)
        p.close()

    def test_native_rnnoise_is_reused_between_frames_and_reset(self):
        from pathlib import Path
        if not Path('models/rnnoise/rnnoise.dll').exists():self.skipTest('Optional RNNoise asset unavailable')
        from alice_voice.noise import MicrophoneProcessor
        p=MicrophoneProcessor('fair');state=p.state
        try:
            for i in range(100):p.process(AudioFrame(bytes(1024),(i+1)*.032))
            self.assertEqual(p.state,state)
            p.reset();self.assertEqual(p.state,state)
            self.assertEqual(p.pending_samples,0)
        finally:p.close()

    def test_probe_is_bounded_and_keeps_raw_overload_evidence(self):
        from alice_voice.microphone_probe import MicrophoneProbe
        p=MicrophoneProbe();p.start=0
        raw=np.full(512,32767,dtype='<i2').tobytes()
        for i in range(300):
            p.raw_frame(AudioFrame(raw,i*.032))
            p.processed_frame(AudioFrame(bytes(1024),i*.032),.9)
        self.assertTrue(p.ready)
        self.assertEqual(len(p.pcm(False).data),125*1024)
        self.assertEqual(len(p.pcm(True).data),125*1024)
        self.assertGreater(p.snapshot()['clipped'],0)
        p.clear();self.assertEqual(p.snapshot()['raw_frames'],0)

    def test_disabled_name_requires_explicit_hold_and_no_fuzzy_match(self):
        from alice_voice.fair_mode import ActivationGate
        s=self.settings();s.wake_enabled=False
        self.assertIsNone(ActivationGate(s).accept('алиса выключи компьютер',1,1))

    def test_stt_quality_is_per_final_utterance_not_previous_text(self):
        from unittest.mock import Mock
        from alice_voice.recognition import VoskRecognizer
        native=Mock()
        native.FinalResult.side_effect=['{"text":"test","result":[{"conf":0.9},{"conf":0.3}]}','{"text":""}']
        stt=VoskRecognizer(native=native)
        self.assertEqual(stt.finalize(),'test');self.assertAlmostEqual(stt.last_confidence,.6)
        self.assertEqual(stt.finalize(),'');self.assertIsNone(stt.last_confidence)

    def test_hold_does_not_replay_speaker_frames_before_clean_floor(self):
        from alice_voice.fair_mode import HoldToTalk
        h=HoldToTalk();h.press('physical',.1,.5)
        for i in range(1,16):self.assertIsNone(h.feed(AudioFrame(bytes(1024),i*.032),1))
        event=h.feed(AudioFrame(bytes(1024),.544),1)
        self.assertEqual(len(event['frames']),1)
        self.assertGreaterEqual(event['frames'][0].captured_at-.032,.5)

    def test_settings_reject_ambiguous_hotkeys_and_unknown_noise_modes(self):
        from dataclasses import replace
        from alice_voice.config import Settings
        for changes in ({'noise_mode':'stacked'},{'ptt_hotkey':'Space'},{'wake_window_s':60},{'fair_stt_confidence':True}):
            with self.assertRaises(ValueError):replace(Settings(),**changes).validate()

    def test_pending_hold_survives_discontinuity_before_its_first_frame(self):
        from alice_voice.fair_mode import HoldToTalk
        h=HoldToTalk();h.press('new',1,1);h.reset()
        self.assertTrue(h.pending)
        self.assertEqual(h.feed(AudioFrame(bytes(1024),1.04),1)['kind'],'start')

    def test_worker_two_holds_finalize_in_continuous_speech_without_waiting_for_silence(self):
        from time import perf_counter,sleep
        from PyQt6.QtCore import QCoreApplication
        from alice_voice.workers import CaptureWorker,WorkerSignals
        from alice_voice.config import Settings
        from alice_voice.metrics import LatencyMetrics
        app=QCoreApplication.instance() or QCoreApplication([])
        base=perf_counter();signals=WorkerSignals();phrases=[];errors=[];worker=None
        signals.phrase.connect(lambda epoch,p:phrases.append(p))
        signals.error.connect(lambda epoch,e:errors.append(e))
        class Capture:
            def __init__(self,inbox,device):self.inbox=inbox
            def open(self):
                for i in range(31):self.inbox.push(np.full(512,1000,dtype='<i2').tobytes(),base+(i+1)*.032)
            def close(self):pass
        class Vad:
            def __init__(self,path):self.i=0
            def reset(self):pass
            def __call__(self,pcm,rate):
                self.i+=1
                if self.i==10:worker.hold.release(base+.32)
                if self.i==18:worker.hold.press('second',base+.576,base+.3)
                if self.i==24:worker.hold.release(base+.768)
                return .95
        class Stt:
            def __init__(self,path):pass
            def reset(self):pass
            def feed(self,pcm):return 'открой блокнот'
            def finalize(self):return 'открой блокнот'
        worker=CaptureWorker(Settings(fair_mode=True,noise_mode='off'),signals,LatencyMetrics(),0,Capture,Vad,Stt)
        worker.hold.press('first',base+.032,base)
        worker.start()
        try:
            deadline=perf_counter()+2
            while len(phrases)<2 and perf_counter()<deadline:
                app.processEvents();sleep(.005)
            self.assertEqual(errors,[])
            self.assertEqual([p['ptt_token'] for p in phrases if p.get('ptt')],['first','second'])
        finally:worker.stop();worker.join(3)


class FairUI(unittest.TestCase):
    def test_settings_hold_and_probe_close_without_starting_microphone(self):
        from tests.test_main_window import MainWindowTests
        MainWindowTests.run_ui(self,'''
            from unittest.mock import patch
            from PyQt6.QtWidgets import QApplication
            from PyQt6.QtCore import Qt
            from PyQt6.QtTest import QTest
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            from alice_voice.ui.dialogs import SettingsDialog
            from alice_voice.ui.microphone_test import MicrophoneTestDialog
            app=QApplication([])
            c=VoiceSessionController(Settings(fair_mode=True))
            w=MainWindow(c);w.show()
            with patch.object(c,'begin_ptt') as begin, patch.object(c,'end_ptt') as end:
                with patch('alice_voice.fair_mode.hotkey_down',side_effect=[True,True,False]):
                    w._poll_ptt_key();w._poll_ptt_key();w._poll_ptt_key()
                begin.assert_called_once();end.assert_called_once()
            with patch('alice_voice.ui.dialogs.MicrophoneDiscovery.start'):
                d=SettingsDialog(c.settings,w)
                d.inputs['noise_mode'].setCurrentIndex(2)
                saved=d.values()
                assert saved.noise_mode=='fair' and saved.fair_mode
                assert 'ptt_hotkey' in d.inputs and not saved.fair_continue
                d.close()
            runtime=c.runtime
            w._open_mini();assert w.mini.controller is c and w.mini.ptt.isEnabled()
            w._restore_main();assert c.runtime is runtime
            d=MicrophoneTestDialog(c.settings,c,w)
            d.show();app.processEvents();d.reject();app.processEvents()
            assert not d.isVisible() and not c.testing_microphone and c.capture is None
            w.allow_close=True;w.close();c.shutdown()
        ''')


class FairControllerTests(unittest.TestCase):
    def setUp(self):
        from PyQt6.QtCore import QCoreApplication
        from unittest.mock import Mock
        from alice_voice.controller import VoiceSessionController
        from alice_voice.config import Settings
        self.app=QCoreApplication.instance() or QCoreApplication([])
        self.c=VoiceSessionController(Settings(fair_mode=True,owner_only=True))
        self.c.timer.stop();self.c.mic_state='listening';self.c.runtime=Mock()
        self.c._noisy=Mock()

    def tearDown(self):
        self.c.runtime=None;self.c.shutdown();self.c._poll()

    def phrase(self,text,**kwargs):
        from time import perf_counter
        from uuid import uuid4
        now=perf_counter()
        return dict(id=uuid4().hex,text=text,start=now,speech_end=now+.8,ready=now+1,
                    owner_verified=kwargs.pop('owner_verified',True),confidence=.9,**kwargs)

    def test_bystander_and_unverified_command_never_submit(self):
        self.c._phrase(0,self.phrase('открой блокнот'))
        self.c._phrase(0,self.phrase('алиса открой блокнот',owner_verified=False))
        self.c.runtime.submit.assert_not_called()

    def test_inline_name_submits_only_final_command(self):
        self.c._phrase(0,self.phrase('алиса открой блокнот'))
        self.assertEqual(self.c.runtime.submit.call_args.args[1],'открой блокнот')

    def test_ptt_is_explicit_one_use_authorization_not_a_second_power_confirmation(self):
        from time import perf_counter
        self.c.ptt_tokens['physical']=perf_counter()+20
        self.c._phrase(0,self.phrase('выключи компьютер',ptt=True,ptt_token='physical',owner_verified=False))
        self.assertEqual(self.c.runtime.submit.call_args.args[1],'выключи компьютер')
        self.assertNotIn('physical',self.c.ptt_tokens)
        self.c.cancel_response(preserve_capture=True)
        self.c._phrase(0,self.phrase('закрой окно',ptt=True,ptt_token='physical',owner_verified=False))
        self.assertEqual(self.c.runtime.submit.call_count,1)

    def test_speaker_playback_and_partials_cannot_activate(self):
        from time import perf_counter
        self.c.echo.start(perf_counter()-.1)
        self.c._phrase(0,self.phrase('алиса выключи компьютер'))
        self.c._voice_evidence(0,dict(self.phrase('алиса'),stable=True))
        self.c.runtime.submit.assert_not_called()
        self.assertEqual(self.c.fair_gate.deadline,0)

    def test_unclear_command_never_runs(self):
        p=self.phrase('алиса закрой окно');p['confidence']=.2
        self.c._phrase(0,p)
        self.c.runtime.submit.assert_not_called();self.c._noisy.assert_called_once()

    def test_physical_hold_interrupt_keeps_preroll_even_over_previous_busy_interval(self):
        from time import perf_counter
        now=perf_counter()
        self.c.busy_intervals.append((now-2,now))
        self.c.accept_after=now
        self.c.ptt_tokens['physical']=now+10
        p=self.phrase('открой блокнот',ptt=True,ptt_token='physical',owner_verified=False)
        p['start']=now-.25
        self.c._phrase(0,p)
        self.c.runtime.submit.assert_called_once()

    def test_noisy_notice_is_not_presented_as_user_speech_in_history(self):
        from alice_voice.fair_mode import LocalReply
        self.c.request_text(LocalReply('Здесь шумно. Повторите.'))
        self.assertEqual(self.c.recognized,'')
        self.assertIsInstance(self.c.pending_text[1],LocalReply)
        self.c.producer_done=True
        self.c._finish_if_done()
        self.assertEqual(list(self.c.completed_turns),[])
