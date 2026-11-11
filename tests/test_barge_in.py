"""Barge-in contracts. Synthetic PCM and fake adapters, no live microphone."""
import asyncio
from dataclasses import replace
from threading import Event
from time import perf_counter
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtMultimedia import QAudio

from alice_voice.config import Settings
from alice_voice.controller import VoiceSessionController
from alice_voice.metrics import LatencyMetrics
from alice_voice.playback import PCMPlayback
from alice_voice.speech import PCM
from alice_voice.workers import DialogRuntime, WorkerSignals
from alice_voice.session import SessionGuard
from audio_fakes import PushSink

APP = QCoreApplication.instance() or QCoreApplication([])


class VoiceInterruptionTests(unittest.TestCase):
    def setUp(self):
        self.c = VoiceSessionController(Settings(headphones=True))
        self.c.timer.stop()
        self.c.runtime = Mock()
        self.c.playback = Mock()
        self.c.capture = Mock(generation=0)
        self.c.capture.inbox.revision = 0
        self.c.capture.inbox.damaged.is_set.return_value = False
        self.c.mic_state = 'listening'
        self.old = self.c.guard.begin('old')

    def tearDown(self):
        self.c.runtime = self.c.capture = None
        self.c.shutdown()
        self.c._poll()

    def phrase(self, text, identity='new', **changes):
        now = perf_counter()
        return dict(id=identity, text=text, start=now - .6, audio_start=now - .8,
                    speech_end=now - .1, ready=now, generation=0, revision=0,
                    **changes)

    def test_stop_words_cancel_generation_and_leave_microphone_on(self):
        for text in ('Стоп!', 'ЗАМОЛЧИ', 'Хватит.'):
            with self.subTest(text=text):
                self.c._phrase(0, self.phrase(text, text))
                self.assertIsNone(self.c.guard.active)
                self.assertEqual(self.c.mic_state, 'listening')
                self.c.runtime.submit.assert_not_called()
                self.c.guard.begin('old-' + text)
        self.assertEqual(self.c.runtime.cancel.call_count, 3)

    def test_stop_invalidates_pending_whisper_even_without_active_response(self):
        self.c.guard.cancel()
        self.c._phrase(0, self.phrase('стоп'))
        self.c.capture.cancel_refinement.assert_called_once()
        self.c.runtime.submit.assert_not_called()

    def test_queued_partial_preserves_whisper_for_same_interrupting_phrase(self):
        from alice_voice.workers import CaptureWorker
        worker = CaptureWorker(Settings(stt_backend='hybrid'), WorkerSignals(), LatencyMetrics(), 0)
        worker.refinement_generation = 1
        worker.refinement_utterance_id = 'new'
        self.c.capture = worker
        event = self.phrase('найти погода', stable=True, acoustic='independent')
        final = dict(event, text='Найди погоду в Бишкеке', refinement_generation=1)
        self.c._voice_evidence(0, event)
        self.c._phrase(0, final)
        self.assertEqual(worker.refinement_generation, 1)
        self.assertEqual(self.c.runtime.submit.call_args.args[1], 'Найди погоду в Бишкеке')

    def test_older_partial_cannot_cancel_newer_hybrid_phrase(self):
        from alice_voice.workers import CaptureWorker
        self.c.settings = replace(self.c.settings, stt_backend='hybrid')
        worker = CaptureWorker(self.c.settings, WorkerSignals(), LatencyMetrics(), 0)
        worker.refinement_generation = 2
        worker.refinement_utterance_id = 'newer'
        self.c.capture = worker
        self.c._voice_evidence(0, self.phrase('погода', 'older', stable=True, acoustic='independent'))
        self.assertEqual(worker.refinement_generation, 2)
        self.assertTrue(self.c.guard.valid(self.old))
        self.c.runtime.cancel.assert_not_called()

    def test_stop_cannot_be_created_by_semantic_cleanup(self):
        self.c._phrase(0, self.phrase('скажи слово точнее стоп'))
        self.assertEqual(self.c.runtime.submit.call_args.args[1], 'стоп')

    def test_new_request_preserves_start_and_rejects_late_old_pcm(self):
        c = self.c
        c.echo.start(perf_counter() - 2)
        pcm = PCM(bytes(1024), 16000)
        c._pcm(self.old, pcm, perf_counter(), 0)
        c._pcm(self.old, pcm, perf_counter(), 1)
        c._phrase(0, self.phrase('ну поставь таймер на семь ой нет, точнее на восемь'))
        new = c.guard.active
        self.assertNotEqual(new, self.old)
        self.assertEqual(c.runtime.submit.call_args.args,
                         (new, 'поставь таймер на восемь'))
        c.capture.reset_phrase.assert_not_called()
        self.assertFalse(c.pcm_queue)
        self.assertEqual(c.pcm_bytes, 0)
        self.assertIsNone(c.current_chunk)
        c._pcm(self.old, pcm, perf_counter(), 2)
        c._answer(self.old, 'поздний ответ')
        c._dialog_done(self.old)
        c._failed(self.old, 'поздняя ошибка')
        self.assertEqual(c.playback.play.call_count, 1)
        self.assertEqual(c.answer, '')
        self.assertTrue(c.guard.valid(new))

    def test_stable_partial_interrupts_without_reset_and_final_is_next_request(self):
        event = self.phrase('найди погоду', stable=True, acoustic='unknown')
        self.c._voice_evidence(0, event)
        self.c._voice_evidence(0, event)
        self.assertIsNone(self.c.guard.active)
        self.c.runtime.cancel.assert_called_once()
        self.c.capture.reset_phrase.assert_not_called()
        self.c._phrase(0, dict(event, text='найди погоду в Бишкеке'))
        self.assertEqual(self.c.runtime.submit.call_args.args[1], 'найди погоду в Бишкеке')

    def test_vad_without_words_or_unstable_partial_cannot_cancel(self):
        for text, stable in (('', True), ('стоп', False)):
            self.c._voice_evidence(0, self.phrase(text, stable=stable, acoustic='independent'))
        self.c.runtime.cancel.assert_not_called()
        self.assertTrue(self.c.guard.valid(self.old))

    def test_speakers_require_acoustic_and_text_evidence(self):
        c = self.c
        c.settings = replace(c.settings, headphones=False)
        c.answer = 'Сейчас расскажу прогноз погоды.'
        c.echo.start(perf_counter() - 2)
        for acoustic in ('unknown', 'echo'):
            c._voice_evidence(0, self.phrase('стоп', stable=True, acoustic=acoustic))
            self.assertTrue(c.guard.valid(self.old))
        c._voice_evidence(0, self.phrase('стоп', stable=True, acoustic='independent'))
        c.runtime.cancel.assert_called_once()
        self.assertIsNone(c.guard.active)

    def test_assistant_stop_word_does_not_interrupt_on_speakers(self):
        c = self.c
        c.settings = replace(c.settings, headphones=False)
        c.answer = 'Для остановки скажите стоп.'
        c.echo.start(perf_counter() - 2)
        c._voice_evidence(0, self.phrase('стоп', stable=True, acoustic='independent'))
        c._phrase(0, self.phrase('стоп', acoustic='independent'))
        c.runtime.cancel.assert_not_called()
        self.assertTrue(c.guard.valid(self.old))

    def test_headphones_reject_detected_echo_and_assistant_words(self):
        c = self.c
        c.answer = 'Так. Для остановки скажите стоп.'
        c.echo.start(perf_counter() - 2)
        for text, acoustic in (('другая расшифровка', 'echo'),
                               ('так', 'independent'), ('стоп', 'independent')):
            with self.subTest(text=text):
                event = self.phrase(text, text, stable=True, acoustic=acoustic)
                c._voice_evidence(0, event)
                c._phrase(0, event)
                self.assertTrue(c.guard.valid(self.old))
        c.runtime.cancel.assert_not_called()
        c.runtime.submit.assert_not_called()

    def test_headphones_allow_same_words_after_playback_tail(self):
        self.c.answer = 'Стоп.'
        self.c.echo.start(perf_counter() - 4)
        self.c.echo.end(perf_counter() - 3)
        self.c._phrase(0, self.phrase('стоп', acoustic='unknown'))
        self.c.runtime.cancel.assert_called_once()

    def test_speaker_phrase_without_reference_is_suppressed_but_headphones_work(self):
        c = self.c
        c.settings = replace(c.settings, headphones=False)
        c.echo.start(perf_counter() - 2)
        c._phrase(0, self.phrase('замолчи'))
        self.assertTrue(c.guard.valid(self.old))
        c.settings = replace(c.settings, headphones=True)
        c._phrase(0, self.phrase('замолчи', 'headphones'))
        self.assertIsNone(c.guard.active)

    def test_stale_generation_revision_and_epoch_cannot_interrupt(self):
        event = self.phrase('стоп', stable=True, acoustic='independent')
        self.c._voice_evidence(1, event)
        self.c._voice_evidence(0, dict(event, generation=1))
        self.c._voice_evidence(0, dict(event, revision=1))
        self.c.capture.inbox.damaged.set()
        self.c.capture.inbox.damaged.is_set.return_value = True
        self.c._voice_evidence(0, event)
        self.c.runtime.cancel.assert_not_called()

    def test_repeated_control_final_cannot_cancel_next_answer(self):
        event = self.phrase('стоп', stable=True, acoustic='independent')
        self.c._voice_evidence(0, event)
        self.c._phrase(0, event)
        new = self.c.guard.begin('next')
        self.c._voice_evidence(0, event)
        self.c._phrase(0, event)
        self.assertTrue(self.c.guard.valid(new))
        self.c.runtime.cancel.assert_called_once()

    def test_manual_cancel_still_resets_capture_and_is_repeatable(self):
        self.c.cancel_response()
        self.c.cancel_response()
        self.assertEqual(self.c.capture.reset_phrase.call_count, 2)
        self.assertIsNone(self.c.guard.active)
        self.assertEqual(self.c.pcm_bytes, 0)


class AcousticEvidenceTests(unittest.TestCase):
    def test_delayed_gain_changed_echo_and_independent_speech(self):
        from alice_voice.utils.barge_in import PlaybackReference
        ref = PlaybackReference()
        rng = np.random.default_rng(42)
        samples = rng.integers(-8000, 8000, 16000, dtype=np.int16)
        ref.add(PCM(samples.tobytes(), 16000), 10)
        echo = (samples[3200:8320].astype(np.float32) * .4).astype('<i2').tobytes()
        self.assertEqual(ref.classify(echo, 10.52 + .16), 'echo')
        unrelated = rng.integers(-8000, 8000, 5120, dtype=np.int16).tobytes()
        self.assertEqual(ref.classify(unrelated, 10.68), 'independent')
        self.assertEqual(PlaybackReference().classify(unrelated, 10.68), 'unknown')
        self.assertEqual(ref.classify(bytes(10240), 10.68), 'unknown')

    def test_reference_is_bounded_and_old_audio_is_unknown(self):
        from alice_voice.utils.barge_in import PlaybackReference
        ref = PlaybackReference()
        for start in range(10):
            ref.add(PCM(b'\x01\x10' * 16000, 16000), start)
        self.assertEqual(ref.classify(b'\x01\x10' * 5120, 1), 'unknown')


class HardStopTests(unittest.TestCase):
    def test_absent_token_cannot_validate_late_completion(self):
        self.assertFalse(SessionGuard().valid(None))

    def test_reset_discards_device_buffer_and_stale_completion_cannot_finish_new_pcm(self):
        device, old, new = Mock(), PushSink(), PushSink(rate=24000)
        device.isNull.return_value = False
        device.isFormatSupported.return_value = True
        p = PCMPlayback()
        with patch('alice_voice.playback.QMediaDevices.defaultAudioOutput', return_value=device), \
                patch('alice_voice.playback.QMediaDevices.audioOutputs', return_value=[]), \
                patch('alice_voice.playback.QAudioSink', side_effect=[old, new]):
            p.play(PCM(bytes(1024), 16000))
            old_slot = old.stateChanged.slot
            old_slot(QAudio.State.IdleState)
            p.stop()
            self.assertEqual(old.reset_count, 1)
            self.assertIsNone(p.buffer)
            self.assertIsNone(p.storage)
            p.stop()
            self.assertEqual(old.reset_count, 1)
            p.play(PCM(bytes(1024), 24000))
            APP.processEvents()
            self.assertIs(p.sink, new)
            self.assertTrue(p.buffer.isOpen())
            p.stop()


class RuntimeCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_invalidates_submit_before_its_loop_callback_runs(self):
        r = DialogRuntime(Settings(), WorkerSignals(), LatencyMetrics())
        r.loop = asyncio.get_running_loop()
        r.ready.set()
        scheduled, visits = [], []
        async def process(token, *args): visits.append(token)
        r._process = process
        with patch.object(r, 'is_alive', return_value=True), patch.object(
                r, '_schedule', side_effect=lambda fn, *args: scheduled.append((fn, args)) or True):
            self.assertTrue(r.submit(1, 'cancel me before start'))
            r.cancel()
            self.assertTrue(r.submit(2, 'next'))
        for fn, args in scheduled: fn(*args)
        if r.current is not None:
            await asyncio.wait_for(r.current, 1)
        self.assertEqual(visits, [2])

    async def test_three_turn_replacement_waits_for_root_cleanup(self):
        r = DialogRuntime(Settings(), WorkerSignals(), LatencyMetrics())
        r.loop = asyncio.get_running_loop()
        r.ready.set()
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        visits = []
        async def process(token, text, cancel, *args):
            visits.append(token)
            if token == 1:
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cleaning.set()
                    await release.wait()
        r._process = process
        r._start(1, 'one')
        await entered.wait()
        r.cancel()
        r._start(2, 'two')
        await cleaning.wait()
        r.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        r._start(3, 'three')
        latest = r.current
        await asyncio.sleep(0)
        self.assertEqual(visits, [1], 'third turn started while first still owned resources')
        release.set()
        await asyncio.wait_for(latest, 1)
        self.assertEqual(visits, [1, 3])

    async def test_delayed_repeated_cancel_does_not_cancel_replacement(self):
        runtime = DialogRuntime(Settings(), WorkerSignals(), LatencyMetrics())
        runtime.loop = asyncio.get_running_loop()
        runtime.ready.set()
        entered, release = asyncio.Event(), asyncio.Event()
        visits = []
        async def process(token, text, cancel, *args):
            visits.append(token)
            if token == 1:
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release.wait()
            else:
                await asyncio.sleep(0)
        runtime._process = process
        runtime._start(1, 'first')
        await entered.wait()
        scheduled = []
        with patch.object(runtime, '_schedule', side_effect=lambda fn, *args: scheduled.append((fn, args)) or True):
            runtime.cancel()
            runtime.cancel()
        runtime._start(2, 'next')
        replacement = runtime.current
        for fn, args in scheduled: fn(*args)
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(replacement, 1)
        self.assertEqual(visits, [1, 2])
        self.assertFalse(replacement.cancelled())

    async def test_cancellation_closes_both_producers_and_drains_session(self):
        entered, closed = set(), set()
        both = asyncio.Event()
        class Provider:
            async def stream(self, messages, first):
                try:
                    yield 'Первое предложение.'
                    entered.add('llm')
                    if len(entered) == 2: both.set()
                    await asyncio.Event().wait()
                finally:
                    closed.add('llm')
        class Synth:
            async def synthesize(self, text, cancel):
                entered.add('tts')
                if len(entered) == 2: both.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    closed.add('tts')
        r = DialogRuntime(Settings(), WorkerSignals(), LatencyMetrics())
        r.provider, r.synth = Provider(), Synth()
        cancel = Event()
        task = asyncio.create_task(r._process(1, 'question', cancel))
        await asyncio.wait_for(both.wait(), 1)
        session = r.stream_session
        task.cancel()
        await asyncio.wait_for(task, 1)
        self.assertEqual(closed, {'llm', 'tts'})
        self.assertTrue(cancel.is_set())
        self.assertFalse(session.inflight)
        self.assertEqual(session.pcm_bytes, 0)
        self.assertTrue(session.played.is_set())
        self.assertIsNone(r.stream_session)
        self.assertFalse(r.history)

    async def test_cancel_clears_already_emitted_pcm_and_waiting_credits(self):
        emitted = asyncio.Event()
        class Provider:
            async def stream(self, messages, first):
                for index in range(10):
                    yield f'Фраза {index}.'
        class Synth:
            async def synthesize(self, text, cancel):
                return PCM(bytes(1024), 16000)
        r = DialogRuntime(Settings(), WorkerSignals(), LatencyMetrics())
        r.provider, r.synth = Provider(), Synth()
        r.signals.pcm.connect(lambda *args: emitted.set())
        task = asyncio.create_task(r._process(1, 'question', Event()))
        await asyncio.wait_for(emitted.wait(), 1)
        session = r.stream_session
        self.assertTrue(session.inflight)
        task.cancel()
        await asyncio.wait_for(task, 1)
        self.assertFalse(session.inflight)
        self.assertEqual(session.pcm_bytes, 0)
        self.assertTrue(session.played.is_set())
