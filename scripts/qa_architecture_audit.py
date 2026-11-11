"""Reproducible QA measurements. No microphone, keys or screenshots in output.

--live permits small real LLM requests; other HTTP checks use MockTransport.
Screen images and synthesized speech are processed only in RAM.
"""
import argparse
import asyncio
import base64
from dataclasses import replace
import gc
from io import BytesIO
import json
from pathlib import Path
import statistics
import sys
from threading import Event
from time import perf_counter
import weakref

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alice_voice.config import Settings
from alice_voice.utils.disfluency_filter import DisfluencyFilter

SCENARIOS = [
    'Поставь таймер на семь... ой нет, точнее на восемь минут',
    'Открой браузер и найди погоду... хотя нет, забудь, открой блокнот',
    'Эээ, ну короче, типа покажи мне, как написать класс на C#, ну ты понял',
    'Что такое типа данных string?',
]
EXPECTED = [
    'Поставь таймер на восемь минут', 'открой блокнот',
    'покажи мне, как написать класс на C#', 'Что такое типа данных string?',
]


def summary(values):
    ordered = sorted(values)
    index = (len(values) - 1) * .95
    low = int(index)
    return {'n': len(values), 'mean_ms': statistics.mean(values),
            'p50_ms': statistics.median(values),
            'p95_ms': ordered[low] + (ordered[min(low + 1, len(values) - 1)] - ordered[low]) * (index - low),
            'min_ms': min(values), 'max_ms': max(values)}


def filter_check():
    cleaner = DisfluencyFilter()
    results = []
    for index, text in enumerate(SCENARIOS):
        times = []
        for _ in range(100): cleaner.clean(text)
        for _ in range(1000):
            start = perf_counter()
            result = cleaner.clean(text)
            times.append((perf_counter() - start) * 1000)
        results.append({'raw': text, 'clean_query': result, 'expected': EXPECTED[index],
                        'status': 'PASS' if result == EXPECTED[index] else ('PARTIAL' if index == 2 else 'FAIL'),
                        'latency': summary(times)})
    return results


async def vision_check(settings):
    import httpx
    import mss
    from PIL import Image
    from alice_voice.providers import LLMProvider
    from alice_voice.utils.vision import capture_screen_jpeg
    totals, resize, encode, sizes = [], [], [], []
    for _ in range(8):
        start = perf_counter()
        data = capture_screen_jpeg()
        totals.append((perf_counter() - start) * 1000)
        sizes.append(len(data))
    with mss.mss() as capture:
        shot = capture.grab(capture.primary_monitor)
    source_dimensions = shot.size
    for _ in range(8):
        with Image.frombytes('RGB', shot.size, shot.rgb) as image, BytesIO() as buffer:
            start = perf_counter()
            image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
            resize.append((perf_counter() - start) * 1000)
            start = perf_counter()
            image.save(buffer, format='JPEG', quality=75, optimize=True)
            encode.append((perf_counter() - start) * 1000)
    payload_bytes = []
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'supportedGenerationMethods': ['generateContent']})
        payload_bytes.append(len(request.content))
        return httpx.Response(200, content=b'data: {"candidates":[{"content":{"parts":[{"text":"OK."}]}}]}\n\n')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = LLMProvider(replace(settings, provider='gemini'), client, key_loader=lambda: 'qa-fake')
        await provider.generate([{'role': 'user', 'content': 'Посмотри на экран'}], lambda: None, image_jpeg=data)
    with Image.open(BytesIO(data)) as image:
        dimensions = image.size
    del data, shot
    return {'capture_resize_encode_total': summary(totals), 'resize': summary(resize),
            'jpeg_encode': summary(encode), 'source_dimensions': source_dimensions,
            'jpeg_dimensions': dimensions, 'jpeg_bytes_range': [min(sizes), max(sizes)],
            'base64_bytes_last': ((sizes[-1] + 2) // 3) * 4,
            'gemini_http_body_bytes_mock_transport': payload_bytes[0]}


async def live_llm_check(settings):
    import httpx
    from alice_voice.providers import LLMProvider
    results = {}
    for kind in ('gemini', 'ollama'):
        times, requests = [], []
        async def response_hook(response):
            requests.append({'method': response.request.method, 'status': response.status_code})
        test_settings = replace(settings, provider=kind, timeout_s=12, max_tokens=64,
                                max_response_chars=100, history_turns=0)
        async with httpx.AsyncClient(timeout=12, trust_env=False,
                                     event_hooks={'response': [response_hook]}) as client:
            provider = LLMProvider(test_settings, client)
            error_type = None
            for _ in range(3):
                start = perf_counter()
                first = []
                try:
                    async for delta in provider.stream(
                            [{'role': 'user', 'content': 'Сколько будет два плюс два? Ответь одним словом.'}],
                            lambda: first.append((perf_counter() - start) * 1000)):
                        pass
                    times.append(first[0])
                except Exception as error:
                    error_type = type(error).__name__
                    break
            results[kind] = {'ttft_ms_cold_then_warm': times, 'http_statuses': requests,
                             'error_type': error_type, 'model': getattr(test_settings, kind + '_model')}
    return results


async def cancellation_check():
    import httpx
    from PyQt6.QtCore import QCoreApplication
    from alice_voice.metrics import LatencyMetrics
    from alice_voice.providers import LLMProvider, ElevenLabsTTSProvider
    from alice_voice.speech import PCM
    from alice_voice.workers import DialogRuntime, WorkerSignals
    app = QCoreApplication.instance() or QCoreApplication([])
    results = {}
    for mode in ('llm', 'tts'):
        samples, closures, collected, depths = [], [], [], []
        for _ in range(20):
            entered, closed = asyncio.Event(), asyncio.Event()
            class PendingHTTP(httpx.AsyncByteStream):
                async def __aiter__(self):
                    if mode == 'llm':
                        yield b'{"message":{"content":"First sentence."}}\n'
                    entered.set()
                    await asyncio.Event().wait()
                    yield b''
                async def aclose(self): closed.set()
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, stream=PendingHTTP()))) as client:
                signals = WorkerSignals()
                runtime = DialogRuntime(Settings(), signals, LatencyMetrics())
                if mode == 'llm':
                    runtime.provider = LLMProvider(Settings(), client)
                    class Synth:
                        async def synthesize(self, text, cancel): return PCM(bytes(64), 16000)
                    runtime.synth = Synth()
                else:
                    class Producer:
                        async def stream(self, messages, first):
                            first()
                            for i in range(20): yield f'Фраза {i}!'
                    runtime.provider = Producer()
                    runtime.synth = ElevenLabsTTSProvider(
                        Settings(elevenlabs_voice_id='qa'), client=client, key_loader=lambda: 'qa-fake')
                task = asyncio.create_task(runtime._process(1, 'qa', Event()))
                runtime.current = task
                runtime.loop = asyncio.get_running_loop()
                runtime.ready.set()
                signals.pcm.connect(lambda token, pcm, when, sequence: runtime._release_pcm(token, sequence))
                await asyncio.wait_for(entered.wait(), 1)
                await asyncio.sleep(0)
                queue = task.get_coro().cr_frame.f_locals['sentences']
                depths.append(queue.qsize())
                queue_ref = weakref.ref(queue)
                del queue
                start = perf_counter()
                runtime.cancel()
                await asyncio.wait_for(task, 1)
                samples.append((perf_counter() - start) * 1000)
                closures.append(closed.is_set())
                gc.collect()
                collected.append(queue_ref() is None)
                assert runtime.stream_session is None and not runtime.history
        results[mode] = {'cancel_to_task_done': summary(samples), 'all_http_responses_closed': all(closures),
                         'all_local_queues_collected': all(collected), 'queue_depth_before_cancel_max': max(depths)}
    return results


def security_check(settings):
    raw = json.loads(Path('settings.json').read_text(encoding='utf-8'))
    result = {'elevenlabs_key_field_in_json': 'elevenlabs_api_key' in raw,
              'elevenlabs_key_nonempty_in_json': bool(raw.get('elevenlabs_api_key', '').strip()),
              'gemini_key_field_in_json': any('key' in key.lower() and 'gemini' in key.lower() for key in raw)}
    try:
        from alice_voice.secrets import load_gemini_key
        result['gemini_credential_available'] = bool(load_gemini_key())
    except RuntimeError:
        result['gemini_credential_available'] = False
    return result


async def main_async(live):
    settings = Settings.load('settings.json')
    result = {'date': '2026-10-21', 'configuration': {key: getattr(settings, key) for key in (
        'provider', 'gemini_model', 'ollama_model', 'synthesizer', 'silence_ms', 'min_speech_ms')},
        'filter': filter_check(), 'security': security_check(settings)}
    result['vision'] = await vision_check(settings)
    result['cancellation_mock_http'] = await cancellation_check()
    if live:
        result['live_llm'] = await live_llm_check(settings)
    path = Path('docs/qa-architecture-results.json')
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    asyncio.run(main_async(args.live))
