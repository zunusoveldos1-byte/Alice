import asyncio
import json
from threading import Event
from time import perf_counter
import unittest
from unittest.mock import Mock, patch

import httpx
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtMultimedia import QAudio

from alice_voice.config import Settings
from alice_voice.controller import VoiceSessionController
from alice_voice.metrics import LatencyMetrics
from alice_voice.playback import PCMPlayback
from alice_voice.providers import LLMProvider
from alice_voice.speech import PCM
from alice_voice.workers import DialogRuntime, WorkerSignals
from audio_fakes import PushSink

APP = QCoreApplication.instance() or QCoreApplication([])


class SentenceTests(unittest.TestCase):
    def test_markdown_link_split_across_deltas_is_cleaned_as_one_fragment(self):
        from alice_voice.streaming import SentenceChunker
        from alice_voice.speech import clean_speech
        chunker = SentenceChunker()
        self.assertEqual(chunker.feed('Смотри [источник](https://example.'), [])
        chunks = chunker.feed('com). Ещё предложение.') + chunker.finish()
        self.assertEqual([clean_speech(chunk) for chunk in chunks], ['Смотри источник.', 'Ещё предложение.'])

    def test_sentences_across_deltas_numbers_and_abbreviations(self):
        from alice_voice.streaming import SentenceChunker
        chunker = SentenceChunker()
        self.assertEqual(chunker.feed('При'), [])
        self.assertEqual(chunker.feed('вет! Как дела?'), ['Привет!', 'Как дела?'])
        self.assertEqual(chunker.feed(' Цена 3.'), [])
        self.assertEqual(chunker.feed('14 руб. Это пример.'), ['Цена 3.14 руб. Это пример.'])
        self.assertEqual(chunker.feed(' Хвост без точки'), [])
        self.assertEqual(chunker.finish(), ['Хвост без точки'])
        self.assertEqual(chunker.finish(), [])


class ProviderStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_network_stall_still_times_out_and_closes_response(self):
        from alice_voice.providers import ProviderError
        class Stream(httpx.AsyncByteStream):
            closed = False
            async def __aiter__(self):
                yield b'{"message":{"content":"First."}}\n'
                await asyncio.Event().wait()
            async def aclose(self): self.closed = True
        source = Stream()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                httpx.Response(200, stream=source))) as client:
            provider = LLMProvider(Settings(timeout_s=1), client)
            stream = provider.stream([], lambda: None)
            self.assertEqual(await anext(stream), 'First.')
            with self.assertRaises(ProviderError): await anext(stream)
        self.assertTrue(source.closed)

    async def test_stream_respects_character_cap(self):
        line = json.dumps({'message': {'content': 'A' * 500}}).encode() + b'\n'
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                httpx.Response(200, content=line))) as client:
            provider = LLMProvider(Settings(max_response_chars=32), client)
            self.assertEqual(''.join([part async for part in provider.stream([], lambda: None)]), 'A' * 32)

    async def test_consumer_pause_does_not_consume_network_timeout(self):
        def handler(request):
            return httpx.Response(200, content=b'{"message":{"content":"First."}}\n{"message":{"content":" Last."}}\n')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = LLMProvider(Settings(timeout_s=1), client)
            stream = provider.stream([], lambda: None)
            try:
                self.assertEqual(await anext(stream), 'First.')
                await asyncio.sleep(1.1)
                self.assertEqual(await anext(stream), ' Last.')
            finally:
                await stream.aclose()

    async def test_whitespace_only_stream_is_rejected(self):
        from alice_voice.providers import ProviderError
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                httpx.Response(200, content=b'{"message":{"content":"   "}}\n'))) as client:
            with self.assertRaises(ProviderError):
                await LLMProvider(Settings(), client).generate([], lambda: None)

    async def test_first_delta_before_http_finishes_and_close_on_consumer_exit(self):
        release = asyncio.Event()
        class Stream(httpx.AsyncByteStream):
            closed = False
            async def __aiter__(self):
                yield b'{"message":{"content":"First."}}\n'
                await release.wait()
                yield b'{"message":{"content":" Last."}}\n'
            async def aclose(self): self.closed = True
        source = Stream()
        def handler(request):
            self.assertTrue(json.loads(request.content)['stream'])
            return httpx.Response(200, stream=source)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = LLMProvider(Settings(), client)
            stream = provider.stream([], lambda: None)
            self.assertEqual(await asyncio.wait_for(anext(stream), 1), 'First.')
            self.assertFalse(release.is_set())
            await stream.aclose()
        self.assertTrue(source.closed)

    async def test_gemini_stream_uses_header_and_filters_thoughts(self):
        def handler(request):
            self.assertEqual(request.headers['x-goog-api-key'], 'fake-key')
            if request.method == 'GET':
                return httpx.Response(200, json={'supportedGenerationMethods': ['generateContent']})
            self.assertIn(':streamGenerateContent', str(request.url))
            data = {'candidates': [{'content': {'parts': [{'text': 'hidden', 'thought': True}, {'text': 'Hello.'}]}}]}
            return httpx.Response(200, content=('data: ' + json.dumps(data) + '\n\n').encode())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = LLMProvider(Settings(provider='gemini'), client, lambda: 'fake-key')
            self.assertEqual([delta async for delta in provider.stream([], lambda: None)], ['Hello.'])


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_lmstudio_pcm_before_http_end_and_repeated_cancel_clears_session(self):
        from alice_voice.secrets import MissingCredentialError
        started, closed = asyncio.Event(), asyncio.Event()
        errors, packets = [], []
        class Source(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"First complete sentence!"}}]}\n\n'
                await asyncio.Event().wait()
            async def aclose(self):
                closed.set()
        class Synth:
            async def synthesize(self, text, cancel):
                return PCM(bytes(1024), 16000)
        def no_key():
            raise MissingCredentialError('fixture')
        settings = Settings(provider='lmstudio', lmstudio_model='actual/id')
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Source()))) as client:
            signals = WorkerSignals()
            runtime = DialogRuntime(settings, signals, LatencyMetrics())
            runtime.provider = LLMProvider(settings, client, lmstudio_token_loader=no_key)
            runtime.synth = Synth()
            signals.failed.connect(lambda token, message: errors.append(message))
            def pcm(*packet):
                packets.append(packet)
                started.set()
            signals.pcm.connect(pcm)
            cancel = Event()
            task = asyncio.create_task(runtime._process(1, 'preserve beginning', cancel))
            await asyncio.wait_for(started.wait(), 1)
            self.assertFalse(closed.is_set(), 'first PCM must precede HTTP completion')
            task.cancel()
            task.cancel()
            await asyncio.wait_for(task, 1)
            self.assertTrue(cancel.is_set())
            self.assertTrue(closed.is_set())
            self.assertIsNone(runtime.stream_session)
            self.assertEqual(list(runtime.history), [])
            self.assertEqual(errors, [])
            self.assertEqual(len(packets), 1)

    async def test_pcm_byte_budget_failure_never_commits_history(self):
        errors, emitted = [], []
        class Provider:
            async def stream(self, messages, first): yield 'Текст.'
        class Synth:
            async def synthesize(self, text, cancel): return PCM(bytes(1024), 16000)
        signals = WorkerSignals()
        runtime = DialogRuntime(Settings(), signals, LatencyMetrics())
        runtime.provider, runtime.synth = Provider(), Synth()
        signals.failed.connect(lambda token, message: errors.append(message))
        signals.pcm.connect(lambda *args: emitted.append(args))
        with patch('alice_voice.workers.MAX_PCM_BYTES', 512):
            await asyncio.wait_for(runtime._process(1, 'test', Event()), 1)
        self.assertTrue(errors)
        self.assertEqual(emitted, [])
        self.assertEqual(list(runtime.history), [])

    async def test_long_playback_pause_resumes_without_stranding_pipeline(self):
        first_three = asyncio.Event()
        emitted, errors = [], []
        resume = False
        def handler(request):
            lines = [json.dumps({'message': {'content': f'Фраза {i}!'}}) for i in range(12)]
            return httpx.Response(200, content=('\n'.join(lines) + '\n').encode())
        class Synth:
            async def synthesize(self, text, cancel): return PCM(bytes(1024), 16000)
        signals = WorkerSignals()
        runtime = DialogRuntime(Settings(timeout_s=1), signals, LatencyMetrics())
        runtime.synth = Synth()
        def ready(token, pcm, when, sequence):
            emitted.append(sequence)
            if len(emitted) == 3: first_three.set()
            if resume: runtime._release_pcm(token, sequence)
        signals.pcm.connect(ready)
        signals.failed.connect(lambda token, message: errors.append(message))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime.provider = LLMProvider(runtime.settings, client)
            task = asyncio.create_task(runtime._process(1, 'question', Event()))
            try:
                await asyncio.wait_for(first_three.wait(), 1)
                await asyncio.sleep(1.1)
                self.assertEqual(len(emitted), 3)
                self.assertFalse(task.done())
                resume = True
                for sequence in list(emitted): runtime._release_pcm(1, sequence)
                await asyncio.wait_for(task, 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(errors, [])
        self.assertEqual(len(emitted), 12)
        self.assertEqual(len(runtime.history), 2)

    async def test_first_pcm_precedes_end_of_llm_and_history_waits_for_playback(self):
        gate, first_pcm = asyncio.Event(), asyncio.Event()
        emitted, done, synthesized = [], [], []
        class Provider:
            async def stream(self, messages, first):
                first()
                yield 'Первое предложение.'
                await gate.wait()
                yield ' Второе предложение.'
        class Synth:
            async def synthesize(self, text, cancel):
                synthesized.append(text)
                return PCM(bytes(1024), 16000)
        signals = WorkerSignals()
        runtime = DialogRuntime(Settings(), signals, LatencyMetrics())
        runtime.provider, runtime.synth = Provider(), Synth()
        def pcm(token, value, when, sequence):
            emitted.append(sequence)
            first_pcm.set()
        signals.pcm.connect(pcm)
        signals.dialog_done.connect(done.append)
        task = asyncio.create_task(runtime._process(1, 'question', Event()))
        try:
            await asyncio.wait_for(first_pcm.wait(), 1)
            self.assertEqual(synthesized, ['Первое предложение.'])
            self.assertFalse(gate.is_set())
            self.assertEqual(list(runtime.history), [])
            gate.set()
            for _ in range(20):
                await asyncio.sleep(.005)
                if len(emitted) == 2: break
            self.assertEqual(len(emitted), 2)
            self.assertEqual(done, [])
            for sequence in emitted:
                runtime._release_pcm(1, sequence)
            await asyncio.wait_for(task, 1)
            self.assertEqual(done, [1])
            self.assertEqual(len(runtime.history), 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_backpressure_duplicate_credits_and_cancellation(self):
        emitted, synthesized = [], []
        third, fourth = asyncio.Event(), asyncio.Event()
        class Provider:
            async def stream(self, messages, first):
                first()
                for i in range(20): yield f'Фраза {i}!'
        class Synth:
            async def synthesize(self, text, cancel):
                synthesized.append(text)
                return PCM(bytes(1024), 16000)
        signals = WorkerSignals()
        runtime = DialogRuntime(Settings(), signals, LatencyMetrics())
        runtime.provider, runtime.synth = Provider(), Synth()
        def ready(token, value, when, sequence):
            emitted.append(sequence)
            if len(emitted) == 3: third.set()
            if len(emitted) == 4: fourth.set()
        signals.pcm.connect(ready)
        cancel = Event()
        task = asyncio.create_task(runtime._process(1, 'question', cancel))
        try:
            await asyncio.wait_for(third.wait(), 1)
            self.assertEqual(len(emitted), 3)
            self.assertEqual(len(synthesized), 3)
            runtime._release_pcm(99, emitted[0])
            await asyncio.sleep(.01)
            self.assertEqual(len(emitted), 3)
            runtime._release_pcm(1, emitted[0])
            runtime._release_pcm(1, emitted[0])
            await asyncio.wait_for(fourth.wait(), 1)
            await asyncio.sleep(.01)
            self.assertEqual(len(emitted), 4)
        finally:
            task.cancel()
            await asyncio.wait_for(task, 1)
        self.assertTrue(cancel.is_set())
        self.assertEqual(list(runtime.history), [])

    async def test_failure_after_first_pcm_cancels_synthesis_and_skips_history(self):
        from alice_voice.providers import ProviderError
        first_pcm = asyncio.Event()
        errors, done = [], []
        class Provider:
            async def stream(self, messages, first):
                first()
                yield 'Первое.'
                await first_pcm.wait()
                raise ProviderError('Ошибка потока.')
        class Synth:
            async def synthesize(self, text, cancel): return PCM(bytes(1024), 16000)
        signals = WorkerSignals()
        runtime = DialogRuntime(Settings(), signals, LatencyMetrics())
        runtime.provider, runtime.synth = Provider(), Synth()
        signals.pcm.connect(lambda *args: first_pcm.set())
        signals.failed.connect(lambda token, message: errors.append(message))
        signals.dialog_done.connect(done.append)
        cancel = Event()
        await asyncio.wait_for(runtime._process(1, 'question', cancel), 1)
        self.assertEqual(errors, ['Ошибка потока.'])
        self.assertEqual(done, [])
        self.assertEqual(list(runtime.history), [])
        self.assertTrue(cancel.is_set())
        self.assertIsNone(runtime.stream_session)


class ControllerStreamingTests(unittest.TestCase):
    def setUp(self):
        self.controller = VoiceSessionController(Settings())
        self.controller.playback = Mock(paused=False)
        self.controller.playback.append.return_value = False
        self.controller.runtime = Mock()
        self.token = self.controller.guard.begin('one')

    def tearDown(self):
        self.controller.runtime = None
        self.controller.shutdown()
        self.controller._poll()

    def test_fifo_and_completion_marker(self):
        c, token = self.controller, self.token
        pcm = PCM(bytes(1024), 16000)
        c._pcm(token, pcm, perf_counter(), 0)
        c._pcm(token, pcm, perf_counter(), 1)
        self.assertEqual(c.playback.play.call_count, 1)
        c._play_finished()
        self.assertEqual(c.playback.play.call_count, 2)
        self.assertTrue(c.guard.valid(token))
        c._play_finished()
        self.assertTrue(c.guard.valid(token))
        c._dialog_done(token)
        self.assertIsNone(c.guard.active)
        self.assertEqual(c.runtime.release_pcm.call_count, 2)

    def test_pause_between_chunks_and_late_pcm_after_cancel(self):
        c, token = self.controller, self.token
        pcm = PCM(bytes(1024), 16000)
        c._pcm(token, pcm, perf_counter(), 0)
        c._play_finished()
        c.toggle_pause()
        c._pcm(token, pcm, perf_counter(), 1)
        self.assertEqual(c.playback.play.call_count, 1)
        c.toggle_pause()
        self.assertEqual(c.playback.play.call_count, 2)
        c.cancel_response()
        c._pcm(token, pcm, perf_counter(), 2)
        c._dialog_done(token)
        self.assertEqual(c.playback.play.call_count, 2)
        self.assertIsNone(c.guard.active)


class BufferLifetimeTests(unittest.TestCase):
    def test_late_state_from_replaced_sink_cannot_close_new_buffer(self):
        device = Mock()
        device.isNull.return_value = False
        device.isFormatSupported.return_value = True
        old_sink, new_sink = PushSink(), PushSink(rate=24000)
        playback = PCMPlayback()
        with patch('alice_voice.playback.QMediaDevices.defaultAudioOutput', return_value=device), \
                patch('alice_voice.playback.QMediaDevices.audioOutputs', return_value=[]), \
                patch('alice_voice.playback.QAudioSink', side_effect=[old_sink, new_sink]):
            playback.play(PCM(bytes(1024), 16000))
            old_callback = old_sink.stateChanged.slot
            playback.play(PCM(bytes(1024), 24000))
            old_callback(QAudio.State.IdleState)
            APP.processEvents()
            self.assertIs(playback.sink, new_sink)
            self.assertTrue(playback.buffer.isOpen())
            playback.stop()

    def test_open_failure_never_reaches_audio_sink(self):
        device = Mock()
        device.isNull.return_value = False
        device.isFormatSupported.return_value = True
        playback = PCMPlayback()
        with patch('alice_voice.playback.QMediaDevices.defaultAudioOutput', return_value=device), \
                patch('alice_voice.playback.QMediaDevices.audioOutputs', return_value=[]), \
                patch('alice_voice.playback.QBuffer.open', return_value=False), \
                patch('alice_voice.playback.QAudioSink') as sink:
            with self.assertRaises(RuntimeError): playback.play(PCM(bytes(1024), 16000))
        sink.assert_not_called()
        self.assertIsNone(playback.buffer)

    def test_buffer_stays_open_until_start_returns(self):
        class Sink(PushSink):
            def start(self):
                buffer = playback.buffer
                assert buffer.isOpen() and buffer.isReadable()
                result = super().start()
                assert buffer.isOpen(), 'buffer closed reentrantly inside start()'
                assert len(buffer.peek(2)) == 2
                return result
        device = Mock()
        device.isNull.return_value = False
        device.isFormatSupported.return_value = True
        sink = Sink()
        playback = PCMPlayback()
        with patch('alice_voice.playback.QMediaDevices.defaultAudioOutput', return_value=device), \
                patch('alice_voice.playback.QMediaDevices.audioOutputs', return_value=[]), \
                patch('alice_voice.playback.QAudioSink', return_value=sink):
            playback.play(PCM(bytes(1024), 16000))
            sink.advance(1024)
            playback._pump()
            APP.processEvents()
        self.assertIsNone(playback.buffer)
        playback.close()


if __name__ == '__main__':
    unittest.main()
