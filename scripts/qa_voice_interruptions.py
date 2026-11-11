"""Opt-in synthetic voice replay -> real QAudioSink StoppedState.

No live microphone, no network, no acoustic loopback. All PCM stays in RAM.
Speaker/headphone labels select policy, not physical hardware. Output muted.
"""
import argparse
from dataclasses import replace
import json
import logging
from pathlib import Path
import sys
from threading import Event, Thread
from time import perf_counter, sleep

import numpy as np
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtMultimedia import QAudio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alice_voice.config import Settings
from alice_voice.controller import VoiceSessionController
from alice_voice.recognition import VoskRecognizer
from alice_voice.speech import PCM, sapi_synthesize
from alice_voice.vad import SileroVAD
from alice_voice.workers import CaptureWorker
from qa_architecture_audit import summary

LOGGER = logging.getLogger(__name__)


def pump(predicate, timeout):
    end = perf_counter() + timeout
    while not predicate() and perf_counter() < end:
        QCoreApplication.processEvents()
        sleep(.001)
    QCoreApplication.processEvents()
    return bool(predicate())


def speech(text):
    import miniaudio
    pcm = sapi_synthesize(text, 'Microsoft Irina Desktop - Russian', Event(), 20)
    data = bytes(miniaudio.convert_frames(miniaudio.SampleFormat.SIGNED16, 1, pcm.sample_rate,
                                         pcm.data, miniaudio.SampleFormat.SIGNED16, 1, 16000))
    return PCM(data, 16000)


def trial(settings, source, carrier, answer, vad, stt, *, headphones, echo_only=False):
    c = VoiceSessionController(replace(settings, headphones=headphones, volume=0))
    c.timer.stop()
    started, stopped, phrases, evidence, errors, submissions = [], [], [], [], [], []
    feed = Event()
    replay_done = Event()
    source_origin = []
    class Runtime:
        cancellations = 0
        def cancel(self): self.cancellations += 1
        def submit(self, token, text):
            submissions.append(text)
            return True
        def release_pcm(self, token, sequence):
            pass  # No producer/backpressure in this pre-synthesized target.
    runtime = Runtime()
    c.runtime = runtime
    class Replay:
        def __init__(self, inbox, device):
            self.inbox, self.done = inbox, Event()
        def open(self):
            def send():
                if not feed.wait(5): return
                prefix = 0 if echo_only else 10 * 1024
                data = bytes(prefix) + source.data + bytes(1024 * 36)
                data += bytes((-len(data)) % 1024)
                base = started[0] if echo_only else perf_counter()
                # The source's first PCM sample above a fixed amplitude floor
                # defines voice onset independently of VAD and STT decisions.
                samples = np.frombuffer(source.data, dtype='<i2').astype(np.int32)
                audible = np.flatnonzero(np.abs(samples) > 100)
                if len(audible):
                    source_origin.append(base + prefix / 32000 + int(audible[0]) / 16000)
                for index in range(0, len(data), 1024):
                    target = base + (index + 1024) / 32000
                    if self.done.wait(max(0, target - perf_counter())): return
                    self.inbox.push(data[index:index + 1024], perf_counter())
                replay_done.set()
            self.thread = Thread(target=send, name='qa-interruption-replay')
            self.thread.start()
        def close(self):
            self.done.set()
            feed.set()
            self.thread.join(2)
    c.signals.phrase.connect(lambda epoch, value: phrases.append(value['text']))
    c.signals.voice_evidence.connect(lambda epoch, value: evidence.append(
        {'text': value['text'], 'acoustic': value['acoustic']}))
    c.notification.connect(errors.append)
    c.playback.error.connect(errors.append)
    c.playback.started.connect(started.append)
    vad.reset()
    stt.reset()
    worker = CaptureWorker(c.settings, c.signals, c.metrics, 0, Replay,
                           lambda path: vad, lambda path: stt)
    worker.playback_reference = c.playback_reference
    c.capture = worker
    worker.start()
    row = {'headphones_policy': headphones, 'echo_only': echo_only}
    sink = None
    try:
        if not pump(lambda: c.mic_state == 'listening' or errors, 6):
            errors.append('capture readiness timeout')
        token = c.guard.begin('already-playing')
        c.answer = answer
        c._pcm(token, carrier, perf_counter(), 0)
        # Observe the actual native endpoint, not cancel_response() duration.
        sink = c.playback.sink
        if sink is not None:
            sink.stateChanged.connect(lambda state: stopped.append(perf_counter())
                                     if state == QAudio.State.StoppedState else None)
        if not pump(lambda: started or errors, 6):
            errors.append('native output start timeout')
        feed.set()
        if not errors:
            if echo_only:
                if not pump(lambda: replay_done.is_set() or bool(errors), len(source.data) / 32000 + 3):
                    errors.append('self-echo replay timeout')
            else:
                if not pump(lambda: stopped or errors, 7):
                    errors.append('voice did not stop output within 7 seconds')
                pump(lambda: bool(phrases), 3)
        row.update(cancelled=bool(runtime.cancellations), recognized=phrases,
                   evidence=evidence, submissions=submissions, errors=errors)
        if source_origin and stopped and not echo_only:
            row['voice_onset_to_sink_stopped_ms'] = (stopped[0] - source_origin[0]) * 1000
        row['output_stopped_state'] = bool(stopped)
        row['pcm_queue_empty'] = not c.pcm_queue and c.current_chunk is None and c.pcm_bytes == 0
        row['buffer_released'] = c.playback.buffer is None
        row['capture_generation'] = worker.generation
    finally:
        worker.stop()
        feed.set()
        worker.join(3)
        c.capture = c.runtime = None
        c.shutdown()
        c._poll()
        c.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-audio', action='store_true', required=True)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--output', type=Path, default=Path('docs/stage2-voice-results.json'))
    args = parser.parse_args()
    if not 1 <= args.repeats <= 20:
        parser.error('--repeats must be 1..20')
    app = QCoreApplication([])
    logging.getLogger('alice_voice').setLevel(logging.CRITICAL)
    settings = Settings()  # No user configuration or real credentials needed.
    vad = SileroVAD(settings.vad_model)
    stt = VoskRecognizer(settings.vosk_model)
    answer = 'Продолжаю подробный ответ о погоде. Сегодня солнечно и тепло. Завтра ожидается дождь.'
    carrier = speech(answer)
    carrier = PCM(carrier.data * 3, carrier.sample_rate)
    sources = {text: speech(text) for text in ('Стоп', 'Замолчи', 'Хватит', 'Найди погоду в Бишкеке')}
    rows = []
    for headphones in (True, False):
        for text, source in sources.items():
            repeats = args.repeats if text == 'Стоп' else 1
            for _ in range(repeats):
                row = trial(settings, source, carrier, answer, vad, stt, headphones=headphones)
                row['source_text'] = text
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
    echo_answer = 'Стоп. Замолчи. Хватит. Это пример голосовых команд.'
    echo = speech(echo_answer)
    for _ in range(2):
        row = trial(settings, echo, echo, echo_answer, vad, stt, headphones=False, echo_only=True)
        row['source_text'] = echo_answer
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    measurements = {}
    for headphones in (True, False):
        values = [row['voice_onset_to_sink_stopped_ms'] for row in rows
                  if row['headphones_policy'] == headphones and row['source_text'] == 'Стоп'
                  and 'voice_onset_to_sink_stopped_ms' in row]
        measurements['headphones' if headphones else 'speakers_policy'] = summary(values) if values else None
    result = {'format_version': 2,
              'method': 'Synthetic Russian SAPI PCM, real-time replay -> real Silero/Vosk/controller/QAudioSink StoppedState',
              'output_volume': 0, 'live_microphone': False, 'acoustic_stop_measured': False,
              'llm_tts_runtime': 'fake; target PCM synthesized by real SAPI before each trial',
              'physical_headphones_and_speakers_tested': False,
              'measurements': measurements, 'rows': rows}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(measurements, ensure_ascii=False), flush=True)
    if any(row['errors'] or (row['cancelled'] == row['echo_only']) for row in rows):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
