"""Incremental PCM, native queue ownership and speech integrity regressions."""
import asyncio
from threading import Event
from time import perf_counter
import unittest
from unittest.mock import Mock, patch

import httpx
import numpy as np
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtMultimedia import QAudio

from alice_voice.config import Settings
from alice_voice.controller import VoiceSessionController
from alice_voice.metrics import LatencyMetrics
from alice_voice.playback import PCMPlayback
from alice_voice.providers import ElevenLabsTTSProvider, LLMProvider
from alice_voice.speech import PCM
from alice_voice.streaming import SentenceChunker
from alice_voice.workers import DialogRuntime, WorkerSignals

APP = QCoreApplication.instance() or QCoreApplication([])


from audio_fakes import PushSink, Signal


class PushPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.device = Mock()
        self.device.isNull.return_value = False
        self.device.isFormatSupported.return_value = True
        self.sink = PushSink()
        self.p = PCMPlayback()
        patches = [patch('alice_voice.playback.QMediaDevices.defaultAudioOutput', return_value=self.device),
                   patch('alice_voice.playback.QMediaDevices.audioOutputs', return_value=[]),
                   patch('alice_voice.playback.QAudioSink', return_value=self.sink)]
        self.factory = patches[-1].start()
        self.addCleanup(patches[-1].stop)
        for item in patches[:-1]: item.start(); self.addCleanup(item.stop)
        self.addCleanup(self.p.close)

    def test_prefetched_pcm_is_contiguous_and_credits_wait_for_render(self):
        done = []
        self.p.finished.connect(lambda: done.append(True))
        first = PCM(b'\x01\x00' * 1600, 16000)
        second = PCM(b'\x02\x00' * 1600, 16000)
        self.p.play(first)
        self.assertTrue(self.p.append(second))
        self.p._pump()
        self.assertEqual(self.sink.received, first.data + second.data)
        self.assertEqual(done, [], 'native acceptance is not rendered audio')
        self.sink.advance(3200)
        self.p._pump()
        self.assertEqual(len(done), 1)
        self.sink.advance(3200)
        self.p._pump()
        self.assertEqual(len(done), 2)
        self.assertEqual(self.sink.start_count, 1)
        self.assertEqual(self.sink.reset_count, 0)

    def test_sink_is_reused_but_old_callback_cannot_finish_a_new_turn(self):
        pcm = PCM(bytes(3200), 16000)
        self.p.play(pcm)
        callback = self.sink.stateChanged.slot
        self.p.stop()
        self.p.play(pcm)
        callback(QAudio.State.IdleState)
        APP.processEvents()
        self.assertIsNotNone(self.p.buffer)
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(self.sink.reset_count, 1)

    def test_idle_before_native_activation_is_not_first_sound_or_completion(self):
        starts, ends = [], []
        self.p.started.connect(starts.append)
        self.p.finished.connect(lambda: ends.append(True))
        original = self.sink.write
        def delayed(data):
            count = original(data)
            self.sink.status = QAudio.State.IdleState
            return count
        self.sink.stateChanged.emit = lambda state: None
        self.sink.write = delayed
        self.p.play(PCM(bytes(3200), 16000))
        self.assertEqual(starts, [])
        self.assertEqual(ends, [])
        self.sink.status = QAudio.State.ActiveState
        self.p._state(self.sink.status)
        self.assertEqual(len(starts), 1)
        self.sink.advance(3200)
        self.p._pump()
        self.assertEqual(ends, [True])

    def test_pause_preserves_unwritten_samples_and_cancellation_discards_all(self):
        pcm = PCM(b'\x03\x00' * 10000, 16000)
        self.p.play(pcm)
        self.p.pause()
        position = self.p.buffer.pos()
        self.p._pump()
        self.assertEqual(self.p.buffer.pos(), position)
        self.p.resume()
        while self.p.buffer is not None:
            self.sink.advance(6400)
            self.p._pump()
        self.assertEqual(self.sink.received, pcm.data)
        self.p.play(pcm)
        self.p.append(pcm)
        self.p.stop()
        self.p.stop()
        self.assertIsNone(self.p.buffer)
        self.assertIsNone(self.p.sink)
        self.assertIsNone(self.p.storage)
        self.assertEqual(self.sink.reset_count, 1)

    def test_controller_prefetch_does_not_replay_pcm_and_late_cancelled_chunks_drop(self):
        c = VoiceSessionController(Settings())
        c.timer.stop()
        c.playback.close()
        c.playback = self.p
        self.p.started.connect(c._play_started)
        self.p.finished.connect(c._play_finished)
        c.runtime = Mock()
        c.speech_end = perf_counter()
        token = c.guard.begin('one')
        pcm = PCM(b'\x05\x00' * 1600, 16000)
        c._pcm(token, pcm, perf_counter(), 0)
        c._pcm(token, pcm, perf_counter(), 1)
        self.p._pump()
        self.sink.advance(3200); self.p._pump()
        self.sink.advance(3200); self.p._pump()
        self.assertEqual(self.sink.received, pcm.data * 2)
        self.assertEqual(c.runtime.release_pcm.call_count, 2)
        c.cancel_response()
        c._pcm(token, pcm, perf_counter(), 2)
        self.assertIsNone(self.p.buffer)
        self.assertEqual(c.pcm_bytes, 0)
        c.runtime = None
        c.shutdown()


class StreamingTTS(unittest.IsolatedAsyncioTestCase):
    async def test_first_pcm_arrives_before_http_tail_and_alignment_is_preserved(self):
        release = asyncio.Event()
        closed = []
        class Audio(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'\x01' * 1921
                await release.wait()
                yield b'\x02' * 1919
            async def aclose(self): closed.append(True)
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, stream=Audio(), headers={'content-type': 'audio/pcm'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            tts = ElevenLabsTTSProvider(Settings(elevenlabs_voice_id='voice'), client=client, key_loader=lambda: 'fixture')
            stream = tts.stream_pcm('Текст.', Event())
            first = await asyncio.wait_for(anext(stream), 1)
            self.assertFalse(release.is_set())
            self.assertEqual(len(first.data) % 2, 0)
            release.set()
            rest = [chunk async for chunk in stream]
        self.assertEqual(b''.join(chunk.data for chunk in [first] + rest), b'\x01' * 1921 + b'\x02' * 1919)
        self.assertTrue(closed)
        self.assertTrue(requests[0].url.path.endswith('/stream'))

    async def test_cancel_and_odd_tail_close_http(self):
        closed = []
        class Audio(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'\x01' * 1920
                await asyncio.Event().wait()
            async def aclose(self): closed.append(True)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Audio()))) as client:
            tts = ElevenLabsTTSProvider(Settings(elevenlabs_voice_id='voice'), client=client, key_loader=lambda: 'fixture')
            stream = tts.stream_pcm('Текст.', Event())
            await anext(stream)
            await stream.aclose()
            self.assertTrue(closed)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'odd'))) as client:
            tts = ElevenLabsTTSProvider(Settings(elevenlabs_voice_id='voice'), client=client, key_loader=lambda: 'fixture')
            with self.assertRaisesRegex(RuntimeError, 'поврежд'):
                _ = [pcm async for pcm in tts.stream_pcm('Текст', Event())]

    async def test_runtime_emits_incremental_pcm_without_waiting_for_full_tts(self):
        emitted, closed = [], []
        gate, first = asyncio.Event(), asyncio.Event()
        class LLM:
            async def stream(self, messages, callback): yield 'Короткий ответ.'
        class Synth:
            async def stream_pcm(self, text, cancel):
                try:
                    yield PCM(bytes(3200), 16000)
                    await gate.wait()
                    yield PCM(bytes(3200), 16000)
                finally: closed.append(True)
        r = DialogRuntime(Settings(), WorkerSignals(), LatencyMetrics())
        r.provider, r.synth = LLM(), Synth()
        def pcm(token, value, when, sequence):
            emitted.append(sequence)
            first.set()
            r._release_pcm(token, sequence)
        r.signals.pcm.connect(pcm)
        task = asyncio.create_task(r._process(1, 'query', Event()))
        await asyncio.wait_for(first.wait(), 1)
        self.assertEqual(emitted, [0])
        self.assertFalse(gate.is_set())
        gate.set()
        await asyncio.wait_for(task, 1)
        self.assertEqual(emitted, [0, 1])
        self.assertTrue(closed)
        self.assertEqual(len(r.history), 2)

    async def test_warmup_reuses_client_and_does_not_generate_a_fake_gemini_turn(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.method == 'GET':
                return httpx.Response(200, json={'supportedGenerationMethods': ['generateContent']})
            return httpx.Response(200, content=b'data: {"candidates":[{"content":{"parts":[{"text":"Reply."}]}}]}\n\n')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            p = LLMProvider(Settings(provider='gemini'), client, key_loader=lambda: 'fixture')
            await p.warmup()
            await p.generate([], lambda: None)
            await p.generate([], lambda: None)
            self.assertIs(p.client, client)
        self.assertEqual([r.method for r in requests], ['GET', 'POST', 'POST'])

    async def test_ollama_warmup_keeps_selected_model_and_surfaces_unavailable_model(self):
        import json
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(404)
        settings = Settings(provider='ollama', ollama_model='chosen-local-model')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = LLMProvider(settings, client)
            with self.assertRaisesRegex(RuntimeError, 'установленную модель'):
                await provider.warmup()
        self.assertEqual(requests, [{'model': 'chosen-local-model', 'prompt': '',
                                    'stream': False, 'keep_alive': '5m'}])
        self.assertEqual(settings.ollama_model, 'chosen-local-model')


class SpeechFragments(unittest.TestCase):
    def test_streamed_playback_reference_retains_echo_evidence_and_real_gaps(self):
        from alice_voice.utils.barge_in import PlaybackReference
        rng = np.random.default_rng(72)
        samples = rng.integers(-8000, 8000, 16000, dtype=np.int16)
        ref = PlaybackReference()
        for offset in range(0, len(samples), 320):
            ref.add(PCM(samples[offset:offset + 320].tobytes(), 16000), 10 + offset / 16000)
        echo = (samples[6400:11520].astype(np.float32) * .4).astype('<i2').tobytes()
        self.assertEqual(ref.classify(echo, 10.72 + .16), 'echo')
        unrelated = rng.integers(-8000, 8000, 5120, dtype=np.int16).tobytes()
        self.assertEqual(ref.classify(unrelated, 10.88), 'independent')
        # A real break must not be compressed into contiguous assistant speech.
        ref.add(PCM(samples[:320].tobytes(), 16000), 13)
        self.assertEqual(ref.classify(unrelated, 13.02), 'unknown')
        rolling = PlaybackReference()
        for offset in range(0, 16000 * 6, 320):
            rolling.add(PCM(samples[offset % 16000:offset % 16000 + 320].tobytes(), 16000),
                        10 + offset / 16000)
        self.assertLessEqual(len(rolling.chunks[-1][1]), rolling.span_samples)
        self.assertEqual(rolling.classify(samples[:5120].tobytes(), 10.32), 'unknown')

    def test_complete_sapi_reference_keeps_the_beginning_before_it_is_played(self):
        from alice_voice.utils.barge_in import PlaybackReference
        rng = np.random.default_rng(73)
        samples = rng.integers(-8000, 8000, 16000 * 8, dtype=np.int16)
        ref = PlaybackReference()
        ref.add(PCM(samples.tobytes(), 16000), 10)
        self.assertEqual(ref.classify(samples[3200:8320].tobytes(), 10.68), 'echo')

    def test_default_endpoint_keeps_internal_russian_speech_pauses(self):
        from alice_voice.recognition import PhraseSegmenter
        from alice_voice.audio import AudioFrame
        for gap in (5, 8, 10, 12, 16, 19):
            with self.subTest(pause_ms=gap * 32):
                seg = PhraseSegmenter(preroll_ms=0)
                pattern = [1] * 12 + [0] * gap + [2] * 12 + [0] * 22
                utterances, received = [], []
                for index, value in enumerate(pattern):
                    update = seg.feed(AudioFrame(value.to_bytes(2, 'little') * 512, (index + 1) * .032),
                                      .9 if value else 0)
                    received.extend(frame.pcm[0] for frame in update.frames)
                    if update.finished: utterances.append(update.utterance_id)
                self.assertEqual(len(utterances), 1)
                self.assertEqual(received.count(1), 12)
                self.assertEqual(received.count(2), 12)

    def test_first_fragment_can_precede_sentence_end_without_losing_words(self):
        chunker = SentenceChunker(early_fragments=True)
        begin = 'Сегодня в Бишкеке солнечно, температура около двадцати градусов'
        fragments = chunker.feed(begin)
        self.assertTrue(fragments)
        rest = chunker.feed(' и дождя не ожидается.') + chunker.finish()
        self.assertEqual(' '.join(fragments + rest), begin + ' и дождя не ожидается.')

    def test_numbers_links_and_code_are_not_split_inside_protected_content(self):
        for text in ('Цена составляет 1 234,50 руб. Подробнее [источник](https://example.com/long/path).',
                     'Пример содержит `очень длинный фрагмент кода с пробелами внутри` и пояснение.'):
            with self.subTest(text=text):
                c = SentenceChunker(early_fragments=True)
                parts = []
                for char in text: parts.extend(c.feed(char))
                parts.extend(c.finish())
                self.assertEqual(' '.join(parts), text)
                self.assertTrue(any('1 234,50' in part for part in parts) if '1 234,50' in text else True)
