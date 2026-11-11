"""Paired text replay. Fixture timings are not native model/acoustic measurements."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sys
from threading import Event
from time import perf_counter, sleep
from unittest.mock import Mock

parser = argparse.ArgumentParser()
parser.add_argument('--root', default=str(Path(__file__).resolve().parents[1]))
parser.add_argument('--output', required=True)
parser.add_argument('--live', action='store_true')
parser.add_argument('--fixed-llm', action='store_true', help='Real TTS/audio with identical scripted text')
args = parser.parse_args()
sys.path.insert(0, args.root)

from PyQt6.QtCore import QCoreApplication
from alice_voice.config import Settings
from alice_voice.context_tools import ToolRouter
from alice_voice.metrics import LatencyMetrics
from alice_voice.silero_tts import SileroSynthesizer
from alice_voice.speech import PCM
from alice_voice.workers import DialogRuntime, WorkerSignals

app = QCoreApplication([])


class FixtureSynth(SileroSynthesizer):
    def __init__(self):
        self.cold = True

    async def prepare(self, cancel, status=lambda _: None):
        if self.cold:
            await asyncio.sleep(.3)
            self.cold = False

    async def synthesize(self, text, cancel):
        await asyncio.sleep(.02)
        return PCM(bytes(3200), 16000)

    async def stream_pcm(self, text, cancel):
        yield await self.synthesize(text, cancel)


class FixtureProvider:
    async def stream(self, messages, first):
        await asyncio.sleep(.05)
        first()
        yield 'Сегодня в Бишкеке солнечно, '
        await asyncio.sleep(.05)
        yield 'температура около двадцати градусов. '
        await asyncio.sleep(.08)
        yield 'Дождя не ожидается.'


async def main():
    settings = replace(Settings.load(Path(__file__).resolve().parents[1] / 'settings.json'), volume=0)
    if args.live:
        settings = replace(settings, max_tokens=96, max_response_chars=400)
    metrics = LatencyMetrics()
    signals = WorkerSignals()
    runtime = DialogRuntime(settings, signals, metrics)
    executor = ThreadPoolExecutor(max_workers=1)
    controller = None
    if args.live:
        from alice_voice.providers import LLMProvider
        from alice_voice.controller import VoiceSessionController
        runtime.provider = FixtureProvider() if args.fixed_llm else LLMProvider(settings)
        runtime.executor = executor
        if settings.synthesizer != 'silero':
            raise RuntimeError('Live benchmark currently expects configured Silero')
        runtime.synth = SileroSynthesizer(settings, executor)
        controller = VoiceSessionController(settings)
        controller.timer.stop()
        controller.runtime = runtime
        runtime.signals = controller.signals
        runtime.metrics = controller.metrics
        metrics = controller.metrics
        runtime.release_pcm = runtime._release_pcm  # Replay uses this asyncio loop, not Thread.run.
    else:
        runtime.provider, runtime.synth = FixtureProvider(), FixtureSynth()
        desktop = Mock()
        desktop.active_window.return_value = {}
        runtime.router = ToolRouter(settings, runtime.provider, desktop=desktop)
        class Memory:
            reads = 0
            def context(self):
                self.reads += 1
                sleep(.04)  # Controlled slow storage, identical before/after.
                return ''
        memory = Memory()
        runtime.router.legacy.memory = memory

    rows = []
    try:
        for index, query in enumerate([('Сколько будет два плюс два?' if args.live and not args.fixed_llm else 'Расскажи про Бишкек.')] * 3):
            started = perf_counter()
            row = {'phase': 'cold' if index == 0 else 'warm', 'query': query}
            packets = []
            if controller:
                token = controller.guard.begin(str(index))
                controller.speech_end = started
                controller.chunk_sequences.clear()
                controller.first_voice_recorded = controller.output_started = controller.producer_done = False
                controller._first_text_recorded = False
                controller._last_pcm_finished = None
                controller.busy_start = started
                starts, finishes = [], []
                controller.playback.started.connect(starts.append)
                def finished(): finishes.append(perf_counter())
                controller.playback.finished.connect(finished)
                errors = []
                controller.notification.connect(errors.append)
            else:
                token = index + 1
            def pcm(token, data, when, sequence):
                packets.append((perf_counter() - started, len(data.data) / data.sample_rate / data.channels / 2))
                if not controller:
                    runtime._release_pcm(token, sequence)
            def answer(token, text):
                if text.strip():
                    row.setdefault('first_text_ms', (perf_counter() - started) * 1000)
            runtime.signals.pcm.connect(pcm)
            runtime.signals.answer.connect(answer)
            task = asyncio.create_task(runtime._process(token, query, Event()))
            async def pump():
                while not task.done():
                    app.processEvents()
                    await asyncio.sleep(.001)
            try:
                await asyncio.wait_for(asyncio.gather(task, pump()), 45 if args.live else 10)
            except TimeoutError:
                row['timeout'] = True
            app.processEvents()  # Deliver dialog_done before accepting the next replay turn.
            row['first_pcm_ms'] = packets[0][0] * 1000 if packets else None
            row['pcm_arrival_gap_ms'] = [round(max(0, packets[i][0] - packets[i-1][0] - packets[i-1][1]) * 1000, 3)
                                          for i in range(1, len(packets))]
            row['packets'] = len(packets)
            row['total_ms'] = (perf_counter() - started) * 1000
            row['metrics'] = metrics.summary()
            if controller:
                row['errors'] = errors
                row['qaudio_first_active_ms'] = row['metrics'].get('speech_end_to_voice', {}).get('mean_ms')
                row['playback_gap_ms'] = [max(0, (starts[i] - finishes[i-1]) * 1000)
                                          for i in range(1, min(len(starts), len(finishes) + 1))]
                controller.playback.started.disconnect(starts.append)
                controller.playback.finished.disconnect(finished)
                controller.notification.disconnect(errors.append)
            else:
                row['storage_reads_total'] = memory.reads
            rows.append(row)
            print(json.dumps({'progress': index + 1, 'first_text_ms': row.get('first_text_ms'), 'first_pcm_ms': row.get('first_pcm_ms'),
                              'timeout': row.get('timeout', False)}, ensure_ascii=True), flush=True)
            runtime.signals.pcm.disconnect(pcm)
            runtime.signals.answer.disconnect(answer)
            with metrics.lock:
                metrics.values.clear()
    finally:
        if args.live and not args.fixed_llm:
            await runtime.provider.close()
        if controller:
            controller.runtime = None
            controller.shutdown()
        executor.shutdown(wait=True, cancel_futures=True)
    return {'mode': 'native_silero_fixed_text_muted' if args.fixed_llm else 'native_ollama_silero_muted' if args.live else 'controlled_fixture',
            'acoustic_verified': False, 'input': 'accepted text replay, no microphone/STT/endpoint delay',
            'rows': rows}


try:
    result = asyncio.run(main())
except Exception as exc:
    result = {'mode': 'live' if args.live else 'fixture', 'error_type': type(exc).__name__}
Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=True))
