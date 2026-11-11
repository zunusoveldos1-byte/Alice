import asyncio
from threading import Event
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock,Mock

from PyQt6.QtCore import QCoreApplication
from alice_voice.config import Settings
from alice_voice.context_tools import ToolRouter
from alice_voice.metrics import LatencyMetrics
from alice_voice.speech import PCM
from alice_voice.workers import DialogRuntime,WorkerSignals
from alice_voice.fast_path import fast_reply

APP=QCoreApplication.instance() or QCoreApplication([])


class FastPhraseTests(unittest.TestCase):
    def test_exact_phrases_allow_case_spaces_and_sentence_punctuation(self):
        for phrase in ('привет',' ПрИвЕт!!! ','здравствуйте','доброе   утро.',
                       'добрый день','добрый вечер','как дела?','спасибо','большое спасибо','благодарю'):
            with self.subTest(phrase=phrase):
                self.assertTrue(fast_reply(phrase))

    def test_commands_negation_quotes_and_extra_words_do_not_match(self):
        for phrase in ('привет, открой блокнот','спасибо и закрой окно','как дела с документом',
                       'не говори привет','скажи привет','«привет»','"привет"',
                       'привет! выключи компьютер','да','нет','стоп','отмена',''):
            with self.subTest(phrase=phrase):
                self.assertIsNone(fast_reply(phrase))


class FastRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self):
        r=DialogRuntime(Settings(),WorkerSignals(),LatencyMetrics())
        r.provider=Mock()
        backend=Mock(); backend.active_window.return_value={}
        r.router=ToolRouter(r.settings,r.provider,desktop=backend,conversation=r.conversation)
        r.router.legacy.context=Mock(return_value='')
        r.router.handle=AsyncMock(return_value=None)
        r.synth=Mock(synthesize=AsyncMock(return_value=PCM(bytes(1024),16000)))
        del r.synth.stream_pcm  # Exercise the selected synthesizer interface.
        r.signals.pcm.connect(lambda token,pcm,ready,sequence:r._release_pcm(token,sequence))
        return r

    async def test_greeting_bypasses_router_context_and_llm_but_uses_tts_and_history(self):
        r=self.runtime()
        stages=[]; failures=[]
        r.signals.stage.connect(lambda token,stage:stages.append(stage))
        r.signals.failed.connect(lambda *args:failures.append(args))
        await asyncio.wait_for(r._process(1,'Привет!',Event()),1)
        self.assertEqual(failures,[])
        r.provider.stream.assert_not_called()
        r.router.handle.assert_not_awaited()
        r.router.desktop.active_window.assert_not_called()
        r.synth.synthesize.assert_awaited_once()
        self.assertEqual(r.synth.synthesize.call_args.args[0],fast_reply('привет'))
        self.assertEqual(list(r.history)[-1]['content'],fast_reply('привет'))
        self.assertNotIn('llm',stages)
        self.assertIn('fast_path_to_first_pcm',r.metrics.summary())

    async def test_nonfast_request_keeps_llm_streaming(self):
        r=self.runtime()
        async def stream(messages,first):
            first(); yield 'Ответ модели.'
        r.provider.stream=Mock(side_effect=stream)
        await asyncio.wait_for(r._process(1,'Привет, расскажи о звёздах.',Event()),1)
        r.router.handle.assert_awaited_once()
        r.provider.stream.assert_called_once()
        self.assertEqual(list(r.history)[-1]['content'],'Ответ модели.')

    async def test_pending_flows_keep_router_priority(self):
        for pending in ('power','selection','name'):
            with self.subTest(pending=pending):
                r=self.runtime()
                if pending=='power': r.router.power.request('shutdown','previous')
                if pending=='selection': r.router.pending_choice={'tool':'select_model','choices':[]}
                if pending=='name': r.router.legacy.pending_name={'turn_id':'previous'}
                from alice_voice.context_tools import ToolResult
                r.router.handle.return_value=ToolResult('clarification','Уточните ответ.')
                await asyncio.wait_for(r._process(1,'привет',Event()),1)
                r.router.handle.assert_awaited_once()
                self.assertEqual(list(r.history)[-1]['content'],'Уточните ответ.')

    async def test_cancelled_fast_speech_does_not_commit_history(self):
        r=self.runtime()
        entered=asyncio.Event()
        async def slow(text,cancel):
            entered.set(); await asyncio.Event().wait()
        r.synth.synthesize=slow
        task=asyncio.create_task(r._process(1,'спасибо',Event()))
        await asyncio.wait_for(entered.wait(),1)
        task.cancel(); await asyncio.wait_for(task,1)
        self.assertEqual(list(r.history),[])
        r.provider.stream.assert_not_called()

    async def test_fast_history_waits_for_actual_playback_completion(self):
        r=self.runtime()
        r.signals.pcm.disconnect()
        arrived=asyncio.Event()
        r.signals.pcm.connect(lambda *args:arrived.set())
        task=asyncio.create_task(r._process(1,'привет',Event()))
        await asyncio.wait_for(arrived.wait(),1)
        self.assertFalse(task.done())
        self.assertEqual(list(r.history),[])
        r._release_pcm(1,0)
        await asyncio.wait_for(task,1)
        self.assertEqual(list(r.history)[-1]['content'],fast_reply('привет'))

    async def test_image_request_never_uses_greeting_fast_path(self):
        r=self.runtime()
        r.settings=replace(r.settings,provider='gemini')
        async def stream(messages,first,**kwargs):
            first(); yield 'Описание картинки.'
        r.provider.stream=Mock(side_effect=stream)
        image=b'\xff\xd8\xfffixture'
        await asyncio.wait_for(r._process(1,'привет',Event(),image_jpeg=image),1)
        r.router.handle.assert_awaited_once()
        r.provider.stream.assert_called_once()
        self.assertEqual(r.provider.stream.call_args.kwargs['image_jpeg'],image)
        self.assertEqual(list(r.history)[-1]['content'],'Описание картинки.')
