"""Silero adapter contracts; fake weights/loader, no torch import or network."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

import numpy as np
from alice_voice.config import Settings
from alice_voice.silero_tts import SileroModelCache, SileroSynthesizer


class SileroTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.executor.shutdown)
        self.path = Path(self.temp.name) / 'v4_ru.pt'
        self.weights = b'fake trusted weights'
        self.digest = hashlib.sha256(self.weights).hexdigest()
        self.model = Mock()
        self.model.apply_tts.return_value = np.array([0, .5, -1, 1], dtype=np.float32)
        self.loader = Mock(return_value=self.model)
        self.download = Mock(side_effect=lambda path, cancel: path.write_bytes(self.weights))
        self.cache = SileroModelCache(Path(self.temp.name), downloader=self.download,
                                      loader=self.loader, expected_sha256=self.digest)

    def synth(self, cancel=None):
        return SileroSynthesizer(Settings(synthesizer='silero'), self.executor, cache=self.cache)

    async def test_warmup_discards_trial_pcm_and_reuses_loaded_model_across_adapters(self):
        self.assertIsNone(await self.synth().warmup(Event()))
        await self.synth().warmup(Event())
        self.assertEqual(self.model.apply_tts.call_count, 2)
        self.loader.assert_called_once()
        pcm = await self.synth().synthesize('Ответ.', Event())
        self.assertTrue(pcm.data)
        self.assertEqual(self.model.apply_tts.call_count, 3)

    async def test_warmup_tracks_voice_and_invalidates_when_model_reloads(self):
        await self.synth().warmup(Event())
        changed = replace(Settings(synthesizer='silero'), silero_voice='baya')
        await SileroSynthesizer(changed, self.executor, cache=self.cache).warmup(Event())
        self.assertEqual(self.model.apply_tts.call_count, 4)
        self.assertEqual(self.model.apply_tts.call_args.kwargs['speaker'], 'baya')
        self.cache.models.clear()
        await self.synth().warmup(Event())
        self.assertEqual(self.model.apply_tts.call_count, 6)
        self.assertEqual(self.loader.call_count, 2)

    async def test_failed_warmup_can_retry_without_reloading_weights(self):
        self.model.apply_tts.side_effect = [RuntimeError('failed'),
                                          np.zeros(2400, dtype=np.float32), np.zeros(2400, dtype=np.float32)]
        with self.assertRaises(RuntimeError):
            await self.synth().warmup(Event())
        await self.synth().warmup(Event())
        await self.synth().warmup(Event())
        self.assertEqual(self.model.apply_tts.call_count, 3)
        self.loader.assert_called_once()

    async def test_cancelled_warmup_does_not_run_inference(self):
        cancel = Event()
        cancel.set()
        with self.assertRaises(asyncio.CancelledError):
            await self.synth().warmup(cancel)
        self.model.apply_tts.assert_not_called()

    async def test_parallel_warmup_runs_trial_pair_once(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = SileroSynthesizer(Settings(), executor, cache=self.cache)
            second = SileroSynthesizer(Settings(), executor, cache=self.cache)
            await asyncio.gather(first.warmup(Event()), second.warmup(Event()))
        self.assertEqual(self.model.apply_tts.call_count, 2)
        self.loader.assert_called_once()

    async def test_cancel_during_trial_skips_second_pass_and_allows_retry(self):
        cancel = Event()
        def render(**kwargs):
            cancel.set()
            return np.zeros(2400, dtype=np.float32)
        self.model.apply_tts.side_effect = render
        with self.assertRaises(asyncio.CancelledError):
            await self.synth().warmup(cancel)
        self.model.apply_tts.assert_called_once()
        self.model.apply_tts.side_effect = None
        await self.synth().warmup(Event())
        self.assertEqual(self.model.apply_tts.call_count, 3)


    async def test_download_then_reuse_memory_and_verified_cache_without_network(self):
        synth = self.synth()
        first = await synth.synthesize('Привет.', Event())
        self.assertEqual((first.sample_rate, first.channels), (24000, 1))
        self.assertEqual(np.frombuffer(first.data, '<i2').tolist(), [0, 16384, -32767, 32767])
        await self.synth().synthesize('Вторая фраза.', Event())
        self.download.assert_called_once()
        self.loader.assert_called_once()
        self.assertEqual(self.path.read_bytes(), self.weights)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ['v4_ru.pt'])
        offline = SileroModelCache(Path(self.temp.name), downloader=Mock(side_effect=AssertionError('network')),
                                   loader=self.loader, expected_sha256=self.digest)
        await SileroSynthesizer(Settings(synthesizer='silero'), self.executor, cache=offline).synthesize('Офлайн.', Event())

    async def test_corrupt_cache_fails_without_loading_untrusted_package(self):
        self.path.write_bytes(b'corrupt')
        with self.assertRaisesRegex(RuntimeError, 'поврежд'):
            await self.synth().synthesize('Текст.', Event())
        self.loader.assert_not_called()
        self.download.assert_not_called()

    async def test_failed_download_does_not_publish_partial_cache(self):
        def fail(path, cancel):
            path.write_bytes(b'partial')
            raise OSError('native failure')
        self.cache.downloader = fail
        with self.assertRaisesRegex(RuntimeError, 'загруз'):
            await self.synth().synthesize('Текст.', Event())
        self.assertFalse(self.path.exists())
        self.assertFalse(list(self.path.parent.glob('*.part')))
        self.loader.assert_not_called()

    async def test_wrong_download_hash_and_cancelled_download_never_publish(self):
        self.cache.downloader = lambda path, cancel: path.write_bytes(b'wrong')
        with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
            await self.synth().synthesize('Текст.', Event())
        self.assertFalse(self.path.exists())
        def cancelled(path, cancel):
            path.write_bytes(self.weights)
            cancel.set()
        self.cache.downloader = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.synth().synthesize('Текст.', Event())
        self.assertFalse(self.path.exists())
        self.assertFalse(list(self.path.parent.glob('*.part')))

    async def test_cancel_before_loading_and_cancel_during_inference_discard_pcm(self):
        cancel = Event(); cancel.set()
        with self.assertRaises(asyncio.CancelledError):
            await self.synth().synthesize('Не запускать.', cancel)
        self.loader.assert_not_called()
        cancel.clear()
        def render(**kwargs):
            cancel.set()
            return np.zeros(2400, dtype=np.float32)
        self.model.apply_tts.side_effect = render
        with self.assertRaises(asyncio.CancelledError):
            await self.synth().synthesize('Отменить.', cancel)
        self.assertEqual(self.model.apply_tts.call_count, 1)

    async def test_prepare_status_and_configuration_errors(self):
        statuses = []
        await self.synth().prepare(Event(), statuses.append)
        self.assertEqual(statuses[-1], 'silero_ready')
        self.assertIn('silero_download', statuses)
        self.assertIn('silero_loading', statuses)
        for changes in ({'silero_model': 'arbitrary'}, {'silero_voice': 'absent'}, {'silero_threads': 0}):
            with self.assertRaises(ValueError): replace(Settings(), **changes).validate()

    async def test_cancel_during_initial_load_keeps_model_for_next_turn(self):
        cancel = Event()
        def load(path, threads):
            cancel.set()
            return self.model
        self.loader.side_effect = load
        with self.assertRaises(asyncio.CancelledError):
            await self.synth().prepare(cancel)
        self.assertIs(self.cache.get(2, Event()), self.model)
        self.loader.assert_called_once()

    async def test_cancel_during_download_does_not_restart_shared_initialization(self):
        cancel = Event()
        def download(path, loading_cancel):
            cancel.set()
            path.write_bytes(self.weights)
        self.cache.downloader = Mock(side_effect=download)
        with self.assertRaises(asyncio.CancelledError):
            await self.synth().prepare(cancel)
        await self.synth().prepare(Event())
        self.cache.downloader.assert_called_once()
        self.loader.assert_called_once()

    async def test_cached_prepare_does_not_emit_loading_or_ready_again(self):
        await self.synth().prepare(Event())
        statuses=[]
        await self.synth().prepare(Event(),statuses.append)
        self.assertEqual(statuses,[])
        self.loader.assert_called_once()

    async def test_parallel_prepare_waits_for_one_load_even_if_first_turn_cancelled(self):
        entered,release=Event(),Event()
        first_cancel=Event()
        def load(path,threads):
            entered.set()
            if not release.wait(3): raise RuntimeError('test loader not released')
            return self.model
        self.loader.side_effect=load
        with ThreadPoolExecutor(max_workers=2) as executor:
            first=asyncio.create_task(SileroSynthesizer(Settings(),executor,cache=self.cache).prepare(first_cancel))
            try:
                for _ in range(200):
                    if entered.is_set(): break
                    await asyncio.sleep(.005)
                self.assertTrue(entered.is_set())
                second=asyncio.create_task(SileroSynthesizer(Settings(),executor,cache=self.cache).prepare(Event()))
                first_cancel.set()
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError): await first
            await second
        self.loader.assert_called_once()

    async def test_failed_initialization_can_retry_and_voice_change_reuses_model(self):
        self.loader.side_effect=[OSError('failed'),self.model]
        with self.assertRaises(RuntimeError): await self.synth().prepare(Event())
        await self.synth().prepare(Event())
        changed=replace(Settings(synthesizer='silero'),silero_voice='baya')
        await SileroSynthesizer(changed,self.executor,cache=self.cache).synthesize('Другой голос.',Event())
        self.assertEqual(self.loader.call_count,2)
        self.assertEqual(self.model.apply_tts.call_args.kwargs['speaker'],'baya')

    async def test_invalid_model_output_and_loader_errors_are_safe(self):
        self.model.apply_tts.return_value = np.array([float('nan')])
        with self.assertRaisesRegex(RuntimeError, 'PCM'):
            await self.synth().synthesize('Текст.', Event())
        self.cache.models.clear()
        self.loader.side_effect = OSError('private/native details')
        with self.assertRaisesRegex(RuntimeError, 'модель') as caught:
            await self.synth().synthesize('Текст.', Event())
        self.assertNotIn('private', str(caught.exception))

    async def test_cancelled_await_drops_late_result_and_queued_inference(self):
        entered, release = Event(), Event()
        def render(**kwargs):
            entered.set()
            release.wait(3)
            return np.zeros(2400, dtype=np.float32)
        self.model.apply_tts.side_effect = render
        cancel = Event()
        task = asyncio.create_task(self.synth().synthesize('Первая.', cancel))
        try:
            for _ in range(200):
                if entered.is_set(): break
                await asyncio.sleep(.005)
            self.assertTrue(entered.is_set())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertTrue(cancel.is_set())
            queued = asyncio.create_task(self.synth().synthesize('Поздняя.', cancel))
            with self.assertRaises(asyncio.CancelledError): await queued
        finally:
            release.set()
        await asyncio.get_running_loop().run_in_executor(self.executor, lambda: None)
        self.assertEqual(self.model.apply_tts.call_count, 1)

    async def test_runtime_cancellation_closes_fragment_pipeline_without_history_or_late_pcm(self):
        from alice_voice.metrics import LatencyMetrics
        from alice_voice.workers import DialogRuntime, WorkerSignals
        from PyQt6.QtCore import QCoreApplication
        app = QCoreApplication.instance() or QCoreApplication([])
        entered, release = Event(), Event()
        def render(**kwargs):
            entered.set(); release.wait(3)
            return np.zeros(2400, dtype=np.float32)
        self.model.apply_tts.side_effect = render
        class LLM:
            async def stream(self, messages, first):
                first()
                yield 'Первая фраза. Следующая фраза.'
        r = DialogRuntime(Settings(synthesizer='silero'), WorkerSignals(), LatencyMetrics())
        r.provider, r.synth = LLM(), self.synth()
        packets = []
        r.signals.pcm.connect(lambda *args: packets.append(args))
        cancel = Event()
        task = asyncio.create_task(r._process(1, 'Запрос.', cancel))
        try:
            for _ in range(200):
                if entered.is_set(): break
                await asyncio.sleep(.005)
            self.assertTrue(entered.is_set())
            task.cancel(); await task
            self.assertTrue(cancel.is_set())
        finally:
            release.set()
        await asyncio.get_running_loop().run_in_executor(self.executor, lambda: None)
        app.processEvents()
        self.assertEqual(packets, [])
        self.assertEqual(self.model.apply_tts.call_count, 1)
        self.assertEqual(len(r.history), 0)

    def test_settings_preserve_existing_engine_and_defaults(self):
        settings = Settings.from_dict({'synthesizer': 'elevenlabs', 'silence_ms': 650})
        self.assertEqual(settings.synthesizer, 'elevenlabs')
        self.assertEqual(settings.silero_voice, 'xenia')
        self.assertEqual(settings.silero_threads, 2)
        self.assertEqual(settings.silero_model, 'v4_ru')

    async def test_long_text_is_split_without_losing_words_and_cancel_stops_next_fragment(self):
        text = 'Это достаточно длинный текст для проверки. ' * 40
        synth = self.synth()
        _ = [pcm async for pcm in synth.stream_pcm(text, Event())]
        spoken = [call.kwargs['text'] for call in self.model.apply_tts.call_args_list]
        self.assertGreater(len(spoken), 1)
        self.assertTrue(all(len(part) <= 240 for part in spoken))
        self.assertEqual(' '.join(spoken), text.strip())
        self.model.apply_tts.reset_mock()
        cancel = Event()
        stream = synth.stream_pcm(text, cancel)
        await anext(stream)
        cancel.set()
        with self.assertRaises(asyncio.CancelledError): await anext(stream)
        await stream.aclose()
        self.assertEqual(self.model.apply_tts.call_count, 1)
