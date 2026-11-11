import asyncio
from dataclasses import replace
from threading import Event
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PyQt6.QtCore import QCoreApplication
from alice_voice.config import Settings
from alice_voice.metrics import LatencyMetrics
from alice_voice.silero_tts import SileroSynthesizer
from alice_voice.speech import PCM
from alice_voice.workers import DialogRuntime, WorkerSignals

APP = QCoreApplication.instance() or QCoreApplication([])


class DialogueRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_thread_warms_voice_before_any_request(self):
        warmed = Event()
        class Cold(SileroSynthesizer):
            def __init__(self, settings, executor): pass
            async def warmup(self, cancel):
                warmed.set()
        provider = Mock(warmup=AsyncMock(), close=AsyncMock())
        r = DialogRuntime(Settings(synthesizer='silero'), WorkerSignals(), LatencyMetrics(), warmup=True)
        with patch('alice_voice.workers.SileroSynthesizer', Cold), \
             patch('alice_voice.workers.LLMProvider', return_value=provider), \
             patch('alice_voice.context_tools.ToolRouter'):
            r.start()
            try:
                self.assertTrue(await asyncio.to_thread(warmed.wait, 2))
                self.assertIsNone(r.current)
                self.assertEqual(list(r.history), [])
            finally:
                r.shutdown()
                await asyncio.to_thread(r.join, 3)
            self.assertFalse(r.is_alive())

    async def test_startup_warms_silero_without_turn_audio_and_overlaps_provider(self):
        r = DialogRuntime(Settings(synthesizer='silero'), WorkerSignals(), LatencyMetrics(), warmup=True)
        voice_started = asyncio.Event()
        turn_cancel = r.cancel_event
        turn_cancel.set()  # Cancelling a turn must not cancel service preparation.
        class Cold(SileroSynthesizer):
            def __init__(self): pass
            async def warmup(self, cancel):
                assert cancel is not turn_cancel
                assert not cancel.is_set()
                voice_started.set()
        async def provider_warmup():
            await asyncio.wait_for(voice_started.wait(), .5)
            raise RuntimeError('provider offline')
        r.synth = Cold()
        r.provider = Mock(warmup=provider_warmup)
        packets, stages = [], []
        r.signals.pcm.connect(lambda *args: packets.append(args))
        r.signals.stage.connect(lambda *args: stages.append(args))
        await asyncio.wait_for(r._warmup(), 1)
        self.assertTrue(voice_started.is_set())
        self.assertIn('tts_warmup', r.metrics.summary())
        self.assertEqual(packets, [])
        self.assertEqual(stages, [])
        self.assertEqual(list(r.history), [])

    async def test_silero_background_failure_does_not_stop_provider_warmup(self):
        r = self.runtime()
        class Broken(SileroSynthesizer):
            def __init__(self): pass
            async def warmup(self, cancel):
                raise RuntimeError('unavailable')
        r.synth = Broken()
        r.provider.warmup = AsyncMock()
        await r._warmup()
        r.provider.warmup.assert_awaited_once()
        self.assertIn('provider_warmup', r.metrics.summary())

    def runtime(self, conversation=None):
        from alice_voice.context_tools import ToolRouter
        r = DialogRuntime(Settings(history_turns=1), WorkerSignals(), LatencyMetrics(), conversation=conversation)
        r.provider = Mock()
        desktop = Mock()
        desktop.active_window.return_value = {}
        r.router = ToolRouter(r.settings, r.provider, desktop=desktop, conversation=r.conversation)
        r.router.legacy.context = lambda: ''
        r.signals.pcm.connect(lambda token, pcm, when, sequence: r._release_pcm(token, sequence))
        class Synth:
            async def synthesize(self, text, cancel):
                return PCM(bytes(1024), 16000)
        r.synth = Synth()
        return r

    async def test_model_switch_keeps_recent_summary_and_task_but_not_generation(self):
        r = self.runtime()
        async def stream(messages, first):
            first()
            yield 'Растения используют энергию света.'
        r.provider.stream = stream
        await r._process(1, 'Расскажи о фотосинтезе.', Event())
        r.conversation.task.selected_file = 'Отчёт.docx'
        new = self.runtime(r.conversation)
        self.assertIsNone(new.current)
        self.assertIsNone(new.stream_session)
        sent = []
        async def more(messages, first):
            sent.extend(messages)
            first()
            yield 'Подробности о фотосинтезе.'
        new.provider.stream = more
        await new._process(2, 'Объясни подробнее.', Event())
        self.assertTrue(any('фотосинтезе' in m['content'] for m in sent[:-1]))
        self.assertEqual(new.conversation.task.selected_file, 'Отчёт.docx')
        self.assertTrue(new.conversation.summary)

    async def test_cold_silero_preparation_overlaps_llm(self):
        r = self.runtime()
        llm_started, prepared = asyncio.Event(), asyncio.Event()
        class Cold(SileroSynthesizer):
            def __init__(self): pass
            async def prepare(self, cancel, status=lambda _: None):
                await asyncio.wait_for(llm_started.wait(), .3)
                prepared.set()
            async def stream_pcm(self, text, cancel):
                assert prepared.is_set()
                yield PCM(bytes(1024), 16000)
        r.synth = Cold()
        async def stream(messages, first):
            llm_started.set()
            first()
            yield 'Готово.'
        r.provider.stream = stream
        failures = []
        r.signals.failed.connect(lambda *args: failures.append(args))
        await asyncio.wait_for(r._process(1, 'Сколько будет два плюс два?', Event()), 1)
        self.assertEqual(failures, [])
        self.assertTrue(prepared.is_set())

    async def test_hidden_stream_never_reaches_tts_or_history(self):
        r = self.runtime()
        spoken = []
        async def stream(messages, first):
            first()
            for delta in ('<thi', 'nk>Секрет. </think>', '```json\n{"tool":"x"}\n```', 'Ответ готов.'):
                yield delta
        class Synth:
            async def synthesize(self, text, cancel):
                spoken.append(text)
                return PCM(bytes(1024), 16000)
        r.provider.stream, r.synth = stream, Synth()
        await r._process(1, 'Сколько будет два плюс два?', Event())
        self.assertEqual(spoken, ['Ответ готов.'])
        self.assertEqual(list(r.history)[-1]['content'], 'Ответ готов.')

    async def test_memory_read_does_not_wait_in_cold_tts_executor(self):
        from concurrent.futures import ThreadPoolExecutor
        r = self.runtime()
        r.executor = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(r.executor.shutdown, wait=True)
        llm_started = Event()
        class Cold(SileroSynthesizer):
            def __init__(self): pass
            async def prepare(self, cancel, status=lambda _: None):
                assert await asyncio.get_running_loop().run_in_executor(r.executor, lambda: llm_started.wait(.3)), 'LLM waited behind cold TTS'
            async def stream_pcm(self, text, cancel):
                yield PCM(bytes(1024), 16000)
        async def stream(messages, first):
            llm_started.set()
            first()
            yield 'Готово.'
        r.synth, r.provider.stream = Cold(), stream
        errors = []
        r.signals.failed.connect(lambda *args: errors.append(args))
        await asyncio.wait_for(r._process(1, 'Сколько будет два плюс два?', Event()), 2)
        self.assertEqual(errors, [])

    async def test_new_topic_resets_old_task_reference(self):
        r = self.runtime()
        r.conversation.task.selected_application = 'Firefox'
        r.conversation.task.goal = 'Найти погоду'
        async def stream(messages, first):
            first()
            yield 'Обсудим физику.'
        r.provider.stream = stream
        await r._process(1, 'Другая тема: расскажи о физике.', Event())
        self.assertEqual(r.conversation.task.selected_application, '')

    async def test_model_identity_error_and_long_summary_never_rewrite_owner(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from alice_voice.memory import FactMemory
        from alice_voice.context_tools import ToolRouter
        with TemporaryDirectory() as directory:
            memory = FactMemory(Path(directory) / 'memory.sqlite3')
            memory.remember('Меня зовут Элдос')
            r = self.runtime()
            r.settings = replace(r.settings, data_directory=directory)
            desktop = Mock(); desktop.active_window.return_value = {}
            r.router = ToolRouter(r.settings, r.provider, desktop=desktop, conversation=r.conversation)
            async def mistaken(messages, first):
                first()
                yield 'Андрей, вот объяснение.'
            r.provider.stream = mistaken
            for token in range(1, 15):
                await r._process(token, 'Объясни подробнее.', Event())
            self.assertTrue(r.conversation.summary)
            self.assertEqual(memory.owner_name(), 'Элдос')
            restarted = self.runtime()
            restarted.settings = replace(restarted.settings, data_directory=directory)
            restarted.router = ToolRouter(restarted.settings, restarted.provider, desktop=desktop,
                                          conversation=restarted.conversation)
            spoken = []
            restarted.signals.answer.connect(lambda token, text: spoken.append(text))
            await restarted._process(20, 'Как меня зовут?', Event())
            self.assertEqual(spoken[-1], 'В профиле владельца сохранено имя Элдос.')
            restarted.provider.stream.assert_not_called()


class ContinuityControllerTests(unittest.TestCase):
    def test_old_action_event_does_not_change_new_turn(self):
        from alice_voice.controller import VoiceSessionController
        c = VoiceSessionController(Settings())
        c.timer.stop()
        old = c.guard.begin('old')
        c.cancel_response()
        new = c.guard.begin('new')
        c._action_event({'turn_id': str(old), 'status': 'done', 'power_pending': True, 'active': True})
        self.assertFalse(c.power_pending)
        self.assertFalse(c.automation_active)
        self.assertTrue(c.guard.valid(new))
        c.shutdown()

    def test_controller_context_survives_retired_runtime(self):
        from alice_voice.controller import VoiceSessionController
        c = VoiceSessionController(Settings())
        c.timer.stop()
        c.conversation.commit('Вопрос', 'Ответ')
        c.conversation.task.goal = 'Открыть документ'
        c.reconfigure(replace(c.settings, ollama_model='qwen3.5:4b'))
        c._poll()
        self.assertEqual(c.conversation.task.goal, 'Открыть документ')
        self.assertEqual(list(c.conversation.recent)[-1]['content'], 'Ответ')
        c.shutdown()
