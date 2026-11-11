"""Real CPU Silero measurements; cached weights, networking blocked, PCM in RAM."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import ctypes
from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import socket
import statistics
import sys
from threading import Event
from time import perf_counter, process_time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alice_voice.config import Settings
from alice_voice.silero_tts import SileroModelCache, SileroSynthesizer, MODEL_SHA256

TEXTS = ['Привет. Я работаю локально.', 'Сегодня в Бишкеке солнечно, температура около двадцати градусов.',
         'Таймер установлен на восемь минут.', 'Сначала проверим настройки, затем продолжим разговор.']


def memory():
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ('peak_rss', 'rss', 'peak_pool', 'pool', 'peak_nonpaged',
                                               'nonpaged', 'pagefile', 'peak_pagefile')]
    data = Counters(); data.cb = ctypes.sizeof(data)
    handle = ctypes.windll.kernel32.GetCurrentProcess
    handle.restype = wintypes.HANDLE
    get = ctypes.windll.psapi.GetProcessMemoryInfo
    get.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not get(handle(), ctypes.byref(data), data.cb): raise ctypes.WinError()
    return {'rss_mib': data.rss / 1048576, 'peak_rss_mib': data.peak_rss / 1048576}


def stats(values):
    ordered = sorted(values); at = (len(values) - 1) * .95; left = int(at)
    return {'n': len(values), 'median': statistics.median(values),
            'p95': ordered[left] + (ordered[min(left + 1, len(values) - 1)] - ordered[left]) * (at - left)}


async def main():
    logging.getLogger('alice_voice').setLevel(logging.CRITICAL)
    # No replacement model or TTS. Fail any attempted outbound connection.
    attempts = []
    def deny(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('Network forbidden in cached-model validation')
    result = {'model': 'v4_ru', 'voice': 'xenia', 'sample_rate': 24000, 'cpu_threads': 2,
              'weights_sha256': MODEL_SHA256, 'llm_used': False, 'acoustic_first_sound_measured': False,
              'network_blocked': True, 'logical_cpus': os.cpu_count(), 'memory_before': memory(), 'rows': []}
    options = replace(Settings(), synthesizer='silero', timeout_s=180)
    executor = ThreadPoolExecutor(max_workers=1)
    cache = SileroModelCache()
    synth = SileroSynthesizer(options, executor, cache=cache)
    try:
        with patch.object(socket.socket, 'connect', deny), patch.object(socket, 'create_connection', deny):
            start, cpu = perf_counter(), process_time()
            states = []
            await synth.prepare(Event(), states.append)
            result['cold_prepare_ms'] = (perf_counter() - start) * 1000
            result['cold_prepare_cpu_ms'] = (process_time() - cpu) * 1000
            result['states'] = states
            start = perf_counter()
            first = await synth.synthesize(TEXTS[0], Event())
            result['cold_first_inference_ms'] = (perf_counter() - start) * 1000
            result['cold_request_to_first_pcm_ms'] = result['cold_prepare_ms'] + result['cold_first_inference_ms']
            result['memory_loaded'] = memory()
            for index in range(20):
                text = TEXTS[index % len(TEXTS)]
                start, cpu = perf_counter(), process_time()
                pcm = await synth.synthesize(text, Event())
                elapsed, used = perf_counter() - start, process_time() - cpu
                row = {'index': index, 'text': text, 'first_pcm_ms': elapsed * 1000,
                       'cpu_ms': used * 1000, 'process_cpu_percent_one_core_100': used / elapsed * 100,
                       'audio_ms': len(pcm.data) / 48000 * 1000, **memory()}
                result['rows'].append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
            # Actual native inference continues after cancel; its result must drop.
            real = cache.models[2]
            entered = Event(); calls = []
            class Counted:
                def apply_tts(self, **kwargs):
                    calls.append(kwargs['text']); entered.set()
                    return real.apply_tts(**kwargs)
            cache.models[2] = Counted()
            cancel = Event()
            packets = []
            async def consume():
                async for pcm in synth.stream_pcm('Это проверка отмены длинного локального ответа. ' * 25, cancel):
                    packets.append(pcm)
            task = asyncio.create_task(consume())
            while not entered.is_set(): await asyncio.sleep(.001)
            start = perf_counter(); task.cancel()
            try: await task
            except asyncio.CancelledError: pass
            result['cancel_await_return_ms'] = (perf_counter() - start) * 1000
            await asyncio.get_running_loop().run_in_executor(executor, lambda: None)
            result['cancel_native_drained_ms'] = (perf_counter() - start) * 1000
            result['cancel_native_calls'] = len(calls)
            result['cancel_late_pcm_count'] = len(packets)
            result['cancel_flag_set'] = cancel.is_set()
            result['network_attempts'] = len(attempts)
            result['memory_final'] = memory()
            result['summary'] = {key: stats([row[key] for row in result['rows']]) for key in
                ('first_pcm_ms', 'cpu_ms', 'process_cpu_percent_one_core_100', 'rss_mib')}
            result['actual_torch'] = __import__('torch').__version__
            result['actual_intraop_threads'] = __import__('torch').get_num_threads()
            result['actual_interop_threads'] = __import__('torch').get_num_interop_threads()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    Path('docs/silero-real-results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key != 'rows'}, ensure_ascii=False), flush=True)
    assert result['network_attempts'] == 0 and result['cancel_native_calls'] == 1 and result['cancel_late_pcm_count'] == 0


if __name__ == '__main__': asyncio.run(main())
