"""Real LM Studio + configured TTS + muted native output, no microphone.

One client-cold request and 20 warm requests with identical prompt/config.
Stops each response after first QAudioSink ActiveState. This is NOT acoustic
first sound or end-of-speech latency. Does not change settings or unload models.
"""
import argparse
import asyncio
from contextlib import aclosing
from dataclasses import replace
import json
from pathlib import Path
import statistics
import sys
from time import perf_counter, sleep

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from alice_voice.config import Settings
from alice_voice.lmstudio import LMStudioProvider
from alice_voice.providers import LLMProvider, ProviderError


def summary(values):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * .95
    low = int(position)
    return {'n': len(values), 'median_ms': statistics.median(values),
            'p95_ms': ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (position - low),
            'fraction_below_1500ms': sum(x < 1500 for x in values) / len(values)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', default='settings.json')
    parser.add_argument('--model', help='Actual ID selected explicitly for diagnostics; no settings write')
    parser.add_argument('--url', help='LM Studio /v1 endpoint; no settings write')
    parser.add_argument('--output', default='docs/lmstudio-real-results.json')
    args = parser.parse_args()
    original = Settings.load(args.settings)
    settings = replace(original, provider='lmstudio', volume=0,
                       lmstudio_model=args.model or original.lmstudio_model,
                       lmstudio_url=args.url or original.lmstudio_url).validate()
    result = {'backend': 'real HTTP, configured TTS and QAudioSink; no fixtures',
              'configured_provider_unchanged': original.provider, 'tts': settings.synthesizer,
              'model': settings.lmstudio_model, 'silence_ms': settings.silence_ms,
              'microphone_used': False, 'acoustic_first_sound_measured': False, 'volume': 0,
              'model_cold_start': 'unverified: model/server state is not reset',
              'client_cold': None, 'warm_rows': [], 'ttft': None, 'submit_to_active': None,
              'speech_end_to_actual_sound': None, 'status': 'unverified'}
    def save():
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    async def preflight():
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3), trust_env=False) as client:
            adapter = LMStudioProvider(settings, client)
            ids = await adapter.models()
            result['available_ids'] = ids
            if not settings.lmstudio_model or settings.lmstudio_model not in ids:
                raise ProviderError('Выберите фактический ID модели явно через --model или настройки.')
            try:
                result['tool_protocol_probe'] = await adapter.probe_tools()
            except (ProviderError, TimeoutError):
                result['tool_protocol_probe'] = 'unverified: model did not complete the protocol probe'
    try:
        asyncio.run(preflight())
    except ProviderError as exc:
        result['blocker'] = str(exc)
        save()
        print(json.dumps(result, ensure_ascii=False))
        return 2
    from PyQt6.QtCore import QCoreApplication
    from alice_voice.controller import VoiceSessionController
    from alice_voice import workers
    app = QCoreApplication.instance() or QCoreApplication([])
    controller = VoiceSessionController(settings)
    current = {}
    errors, active = [], []
    class TimingProvider(LLMProvider):
        def __init__(self, cfg):
            super().__init__(cfg)
            async def sent(request):
                if request.method == 'POST' and request.url.path.endswith('/chat/completions'):
                    current.setdefault('http_sent', perf_counter())
            self.client.event_hooks['request'].append(sent)
        async def stream(self, messages, first_token, image_jpeg=None):
            def first():
                current.setdefault('first_text', perf_counter())
                first_token()
            async with aclosing(super().stream(messages, first, image_jpeg)) as stream:
                async for text in stream:
                    yield text
    def pump(predicate, timeout):
        deadline = perf_counter() + timeout
        while not predicate() and perf_counter() < deadline:
            app.processEvents()
            sleep(.001)
        app.processEvents()
        return bool(predicate())
    def pcm(token, packet, when, sequence):
        if controller.guard.valid(token):
            current.setdefault('first_pcm', when)
    controller.signals.pcm.connect(pcm)
    controller.notification.connect(errors.append)
    controller.playback.started.connect(active.append)
    factory = workers.LLMProvider
    workers.LLMProvider = TimingProvider
    try:
        controller.runtime = workers.DialogRuntime(settings, controller.signals, controller.metrics)
        controller.runtime.start()
        if not pump(lambda: controller.runtime.ready.is_set() or errors, 10):
            raise RuntimeError('runtime startup timeout')
        for index in range(21):
            current = {}
            active.clear(); errors.clear()
            prompt = 'Ответь кратко одним предложением: сколько дней в неделе?'
            token = controller.guard.begin(str(index))
            origin = perf_counter()
            controller.speech_end = origin  # Diagnostic anchor is TEXT submission, not real speech end.
            if not controller.runtime.submit(token, prompt):
                raise RuntimeError('request not submitted')
            pump(lambda: active or errors, settings.timeout_s + 30)
            row = {'index': index, 'errors': list(errors), 'active_seen': bool(active)}
            if active:
                row['submit_to_active_ms'] = (active[0] - origin) * 1000
            if 'first_text' in current and 'http_sent' in current:
                row['ttft_ms'] = (current['first_text'] - current['http_sent']) * 1000
            if 'first_pcm' in current:
                row['submit_to_pcm_ms'] = (current['first_pcm'] - origin) * 1000
            controller.cancel_response()
            if not pump(lambda: controller.runtime.current is None or controller.runtime.current.done(), 10):
                row['errors'].append('cancellation teardown timeout')
            if index == 0:
                result['client_cold'] = row
            else:
                result['warm_rows'].append(row)
            save()
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if not active or row['errors']:
                break
        warm = result['warm_rows']
        result['ttft'] = summary([r['ttft_ms'] for r in warm if 'ttft_ms' in r])
        result['submit_to_active'] = summary([r['submit_to_active_ms'] for r in warm if 'submit_to_active_ms' in r])
        result['status'] = '20 warm samples completed' if len(warm) == 20 and all(r['active_seen'] and not r['errors'] for r in warm) else 'incomplete'
    except Exception as exc:
        result['blocker_error_type'] = type(exc).__name__
    finally:
        controller.shutdown()
        pump(lambda: controller.runtime is None, 15)
        workers.LLMProvider = factory
        save()
    return 0 if result['status'] == '20 warm samples completed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
