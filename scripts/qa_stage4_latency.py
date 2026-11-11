"""Paired native replay benchmark; PCM fixtures live only in parent/child RAM.

Gemini+SAPI diagnostic configuration, muted real QAudioSink, no microphone.
Before: frozen stage3 package. After: current package. Identical PCM/settings.
The parent waits for .build-tmp/stage4-run-after.flag after baseline completes.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter, sleep

WORKSPACE = Path(__file__).resolve().parents[1]
QUERIES = ['Сколько будет два плюс два?', 'Сколько дней в неделе?',
           'Назови столицу Кыргызстана.', 'Сколько месяцев в году?']


def summary(values):
    import statistics
    ordered = sorted(values)
    position = (len(ordered) - 1) * .95
    low = int(position)
    return {'n': len(values), 'median_ms': statistics.median(values),
            'p95_ms': ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (position - low)} if values else None


def worker(root, phase):
    sys.path.insert(0, str(Path(root).resolve()))
    import logging
    from threading import Event, Thread
    from PyQt6.QtCore import QCoreApplication
    from alice_voice.config import Settings
    from alice_voice.controller import VoiceSessionController
    from alice_voice.recognition import VoskRecognizer
    from alice_voice.vad import SileroVAD
    from alice_voice.workers import CaptureWorker, DialogRuntime
    logging.getLogger('alice_voice').setLevel(logging.CRITICAL)
    payload = json.loads(sys.stdin.read())
    cfg = WORKSPACE / '.build-tmp/stage4-before/benchmark-settings.json'
    settings = Settings.load(cfg)
    app = QCoreApplication([])
    boot = perf_counter()
    c = VoiceSessionController(settings)
    starts, errors = [], []
    c.notification.connect(errors.append)
    c.playback.started.connect(starts.append)
    if phase.startswith('native_'):
        from unittest.mock import patch
        import asyncio, httpx
        from alice_voice.providers import LLMProvider
        class FixedStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                await asyncio.sleep(.300)
                parts = (['Сегодня в Бишкеке солнечно, температура', ' около двадцати градусов',
                          ', дождя не ожидается', ' и ветер слабый.']
                         if phase.startswith('native_long_') else ['Four.'])
                for index, part in enumerate(parts):
                    if index:
                        await asyncio.sleep(.150)
                    event = {'candidates': [{'content': {'parts': [{'text': part}]}}]}
                    yield ('data: ' + json.dumps(event, ensure_ascii=False) + '\n\n').encode('utf-8')
            async def aclose(self): pass
        def factory(options):
            def handler(request):
                if request.method == 'GET':
                    return httpx.Response(200, json={'supportedGenerationMethods': ['generateContent']})
                return httpx.Response(200, stream=FixedStream())
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            provider = LLMProvider(options, client=client, key_loader=lambda: 'non-api-fixture')
            provider.owns_client = True
            return provider
        transport_patch = patch('alice_voice.workers.LLMProvider', side_effect=factory)
        transport_patch.start()
    import inspect
    options = {'warmup': True} if 'warmup' in inspect.signature(DialogRuntime).parameters else {}
    c.runtime = DialogRuntime(settings, c.signals, c.metrics, **options)
    c.runtime.start()
    def pump(predicate, timeout):
        deadline = perf_counter() + timeout
        while not predicate() and perf_counter() < deadline:
            app.processEvents()
            sleep(.001)
        app.processEvents()
        return bool(predicate())
    pump(lambda: c.runtime.ready.is_set() or errors, 8)
    begin = perf_counter()
    vad = SileroVAD(settings.vad_model)
    cold_vad = (perf_counter() - begin) * 1000
    begin = perf_counter()
    stt = VoskRecognizer(settings.vosk_model)
    cold_stt = (perf_counter() - begin) * 1000
    class TimedSTT:
        def reset(self): stt.reset()
        def feed(self, pcm):
            begin = perf_counter()
            result = stt.feed(pcm)
            c.metrics.record('stt_feed', perf_counter() - begin)
            return result
        def finalize(self): return stt.finalize()
    rows = []
    for index in range(21):
        turn_started = perf_counter()
        source = payload[index % 4] if index else payload[0]
        raw = base64.b64decode(source['pcm'])
        starts.clear(); errors.clear()
        with c.metrics.lock: c.metrics.values.clear()
        vad.reset(); stt.reset()
        clock = []
        class Replay:
            def __init__(self, inbox, device): self.inbox, self.done = inbox, Event()
            def open(self):
                def feed():
                    origin = perf_counter()
                    clock.append(origin)
                    data = bytes(1024 * 10) + raw + bytes(1024 * 44)
                    data += bytes((-len(data)) % 1024)
                    for offset in range(0, len(data), 1024):
                        target = origin + (offset + 1024) / 32000
                        if self.done.wait(max(0, target - perf_counter())): return
                        self.inbox.push(data[offset:offset + 1024], perf_counter())
                self.thread = Thread(target=feed, name='stage4-voice-replay')
                self.thread.start()
            def close(self):
                self.done.set(); self.thread.join(2)
        capture = CaptureWorker(settings, c.signals, c.metrics, c.epoch, Replay,
                                lambda _: vad, lambda _: TimedSTT())
        capture.playback_reference = c.playback_reference
        c.capture = capture
        capture.start()
        completed = pump(lambda: starts or errors, 28)
        row = {'index': index, 'cold': index == 0, 'query': source['query'],
               'pcm_sha256': source['sha256'], 'recognized': c.recognized,
               'metrics': c.metrics.summary(), 'errors': list(errors), 'active_state': bool(starts)}
        if starts and clock:
            voice_end = clock[0] + .32 + source['voice_end_sample'] / 16000
            row['speech_end_to_active_ms'] = (starts[0] - voice_end) * 1000
            if index == 0: row['process_boot_to_first_active_ms'] = (starts[0] - boot) * 1000
        if not completed: row['errors'].append('replay timeout')
        capture.stop(); capture.join(3)
        c.capture = None
        c.cancel_response()
        pump(lambda: c.runtime.current is None or c.runtime.current.done(), 4)
        rows.append(row)
        print(json.dumps({'phase': phase, **row}, ensure_ascii=False), flush=True)
        if phase == 'before_paced' and index < 20:
            # Stay below the observed cloud rate limit; idle time is excluded
            # from end-of-speech measurements and identical in paired phases.
            pump(lambda: perf_counter() >= turn_started + 14, 14)
    c.shutdown()
    closed = pump(lambda: c.runtime is None, 8)
    warm = [row for row in rows if not row['cold']]
    values = [row['speech_end_to_active_ms'] for row in warm if row['active_state']]
    stages = {}
    for stage in ('endpoint_wait', 'stt_finalize', 'stt_feed', 'speech_end_to_text',
                  'llm_first', 'llm_first_sentence', 'llm_to_first_pcm', 'tts', 'tts_first_pcm', 'pcm_to_playback'):
        items = [row['metrics'][stage]['p50_ms'] for row in warm if stage in row['metrics']]
        stages[stage] = summary(items)
    result = {'phase': phase, 'rows': rows, 'warm_summary': summary(values), 'stages': stages,
              'warm_attempts': 20, 'warm_successes': len(values),
              'under_1500_count': sum(value < 1500 for value in values),
              'under_1500_fraction_all_attempts': sum(value < 1500 for value in values) / 20,
              'cold_vad_ms': cold_vad, 'cold_stt_ms': cold_stt, 'workers_closed': closed,
              'configuration_sha256': hashlib.sha256(cfg.read_bytes()).hexdigest(),
              'diagnostic_override': 'Gemini selected model + SAPI; configured ElevenLabs unavailable',
              'llm_transport': 'controlled HTTP fixture, fixed 300 ms TTFT' if phase.startswith('native_') else 'real Gemini',
              'fixture_response': 'Russian four SSE deltas 300/450/600/750 ms' if phase.startswith('native_long_') else 'Four. in one delta' if phase.startswith('native_') else None,
              'acoustic_first_sound_measured': False, 'output_volume': 0}
    (WORKSPACE / f'docs/stage4-{phase}-results.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker-root')
    parser.add_argument('--phase')
    parser.add_argument('--live-paired', action='store_true')
    parser.add_argument('--native-paired', action='store_true')
    parser.add_argument('--native-long-paired', action='store_true')
    args = parser.parse_args()
    if args.worker_root:
        if args.phase == 'after' and (WORKSPACE / '.build-tmp/stage4-redo-before.flag').exists():
            from io import StringIO
            data = sys.stdin.read()
            result = subprocess.run([sys.executable, __file__, '--worker-root',
                                     str(WORKSPACE / '.build-tmp/stage4-before'), '--phase', 'before_paced'],
                                    input=data, text=True, encoding='utf-8', cwd=WORKSPACE)
            if result.returncode: raise SystemExit(result.returncode)
            print('Paced baseline complete; waiting for final code flag', flush=True)
            while not (WORKSPACE / '.build-tmp/stage4-final-code-ready.flag').exists(): sleep(1)
            sys.stdin = StringIO(data)
        return worker(args.worker_root, args.phase)
    if args.native_long_paired:
        args.native_paired = True
    if not args.live_paired and not args.native_paired:
        parser.error('Select --live-paired (cloud requests) or --native-paired (HTTP fixture)')
    sys.path.insert(0, str(WORKSPACE))
    import numpy as np
    import miniaudio
    from threading import Event
    from alice_voice.speech import sapi_synthesize
    payload = []
    for query in QUERIES:
        pcm = sapi_synthesize(query, 'Microsoft Irina Desktop - Russian', Event(), 20)
        raw = bytes(miniaudio.convert_frames(miniaudio.SampleFormat.SIGNED16, 1, pcm.sample_rate,
                                             pcm.data, miniaudio.SampleFormat.SIGNED16, 1, 16000))
        samples = np.frombuffer(raw, dtype='<i2').astype(np.int32)
        voice_end = int(np.flatnonzero(np.abs(samples) > 100)[-1]) + 1
        raw = raw[:voice_end * 2]
        payload.append({'query': query, 'pcm': base64.b64encode(raw).decode('ascii'),
                        'voice_end_sample': voice_end, 'sha256': hashlib.sha256(raw).hexdigest()})
    data = json.dumps(payload)
    def run(root, phase):
        result = subprocess.run([sys.executable, __file__, '--worker-root', str(root), '--phase', phase],
                                input=data, text=True, encoding='utf-8', cwd=WORKSPACE)
        if result.returncode: raise SystemExit(result.returncode)
    prefix = 'native_long_' if args.native_long_paired else 'native_' if args.native_paired else ''
    run(WORKSPACE / '.build-tmp/stage4-before', prefix + 'before')
    print('Baseline complete; PCM retained only in RAM; waiting for after flag', flush=True)
    flag = WORKSPACE / '.build-tmp/stage4-final-code-ready.flag' if args.native_paired else WORKSPACE / '.build-tmp/stage4-run-after.flag'
    while not flag.exists(): sleep(1)
    run(WORKSPACE, prefix + 'after')
    print('Paired benchmark complete', flush=True)


if __name__ == '__main__': main()
