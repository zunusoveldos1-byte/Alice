"""Real Silero/Vosk/SAPI/Qt audit, with synthetic speech and muted output.

--live-replay additionally sends three short recognized requests to Gemini
through the production controller/runtime, substituting SAPI for unconfigured
ElevenLabs. No microphone or audio/image files are used.
"""
import argparse
from dataclasses import replace
import json
import logging
from pathlib import Path
import sys
from threading import Event, Thread
from time import perf_counter, sleep
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt6.QtCore import QCoreApplication, QEvent
from alice_voice.audio import AudioFrame
from alice_voice.config import Settings
from alice_voice.controller import VoiceSessionController
from alice_voice.recognition import PhraseSegmenter, VoskRecognizer
from alice_voice.speech import sapi_synthesize
from alice_voice.vad import SileroVAD
from alice_voice.workers import CaptureWorker
from qa_architecture_audit import summary

logging.getLogger('alice_voice').setLevel(logging.CRITICAL)


def pump(predicate, timeout):
    deadline = perf_counter() + timeout
    while not predicate() and perf_counter() < deadline:
        QCoreApplication.processEvents()
        sleep(.001)
    QCoreApplication.processEvents()
    return bool(predicate())


def source_pcm(voice):
    import miniaudio
    pcm = sapi_synthesize('Сколько будет два плюс два?', voice, Event(), 15)
    raw = bytes(miniaudio.convert_frames(miniaudio.SampleFormat.SIGNED16, 1, pcm.sample_rate,
                                        pcm.data, miniaudio.SampleFormat.SIGNED16, 1, 16000))
    raw = bytes(1024 * 10) + raw + bytes(1024 * 40)
    raw += bytes((-len(raw)) % 1024)
    return [raw[i:i + 1024] for i in range(0, len(raw), 1024)]


def local_stt(settings, frames):
    start = perf_counter()
    vad = SileroVAD(settings.vad_model)
    vad_load = (perf_counter() - start) * 1000
    start = perf_counter()
    stt = VoskRecognizer(settings.vosk_model)
    stt_load = (perf_counter() - start) * 1000
    times, all_compute, recognized = [], [], []
    for _ in range(5):
        vad.reset(); stt.reset()
        seg = PhraseSegmenter(settings.threshold, settings.silence_ms, settings.preroll_ms,
                              settings.max_phrase_s, settings.min_speech_ms)
        compute = 0
        for index, pcm in enumerate(frames):
            begin = perf_counter()
            update = seg.feed(AudioFrame(pcm, index * .032), vad(pcm, 16000))
            if update.started: stt.reset()
            for frame in update.frames: stt.feed(frame.pcm)
            if update.finished:
                start = perf_counter()
                recognized.append(stt.finalize())
                times.append((perf_counter() - start) * 1000)
            compute += (perf_counter() - begin) * 1000
        all_compute.append(compute)
    return {'source': 'Russian SAPI speech, offline frames, no microphone',
            'vad_load_ms': vad_load, 'vosk_load_ms': stt_load,
            'stt_finalize': summary(times) if times else None,
            'vad_and_stt_compute_per_utterance': summary(all_compute),
            'recognized': recognized,
            'configured_endpoint_wait_ms': ((settings.silence_ms + 31) // 32) * 32}


def native_audio(settings, voice):
    controller = VoiceSessionController(replace(settings, volume=0))
    synthesis, start_audio, stop_times, states = [], [], [], []
    errors = []
    controller.playback.error.connect(errors.append)
    for index in range(5):
        begin = perf_counter()
        pcm = sapi_synthesize('Четыре.', voice, Event(), 15)
        synthesis.append((perf_counter() - begin) * 1000)
        starts = []
        controller.playback.started.connect(starts.append)
        controller.guard.begin(str(index))
        controller.current_chunk = (0, pcm, perf_counter())
        controller.pcm_ready = controller.speech_end = perf_counter()
        start = perf_counter()
        controller.playback.play(pcm, volume=0)
        if not pump(lambda: starts or errors, 6):
            errors.append('Native audio startup timeout')
        if errors:
            controller.playback.stop()
            break
        start_audio.append((starts[0] - start) * 1000)
        controller.pcm_queue.append((1, pcm, perf_counter()))
        controller.pcm_bytes = len(pcm.data)
        sink = controller.playback.sink
        begin = perf_counter()
        controller.cancel_response()
        stop_times.append((perf_counter() - begin) * 1000)
        states.append({'state_after_stop': sink.state().name, 'pcm_queue_size': len(controller.pcm_queue),
                       'buffer_released': controller.playback.buffer is None,
                       'active_token_cleared': controller.guard.active is None})
        controller.playback.started.disconnect(starts.append)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    controller.shutdown(); controller._poll(); controller.deleteLater()
    return {'volume': 0, 'tts_sapi': summary(synthesis),
            'pcm_to_qaudiosink_active_ms_cold_then_warm': start_audio,
            'cancel_response_to_stopped': summary(stop_times) if stop_times else None,
            'stop_checks': states, 'errors': errors}


def voice_interruptions(settings):
    result = {}
    for text, headphones in [('Стоп', False), ('Новый вопрос', True),
                              ('отключайся', False), ('отключайся', True)]:
        c = VoiceSessionController(replace(settings, headphones=headphones))
        c.runtime, c.playback = Mock(), Mock()
        c.mic_state = 'listening'
        c.guard.begin('playing')
        now = perf_counter()
        c.echo.start(now - 2)
        c._phrase(0, dict(id='interrupt', text=text, start=now - .5, ready=now, speech_end=now - .1))
        result[f'{text};headphones={headphones}'] = {'cancel_called': c.runtime.cancel.called,
                                                    'playback_stop_called': c.playback.stop.called}
        c.runtime = None
        c.timer.stop()
        c.deleteLater()
    return result


def live_replay(settings, frames, voice):
    result = []
    class ReplayCapture:
        def __init__(self, inbox, device):
            self.inbox, self.done = inbox, Event()
        def open(self):
            def feed():
                target = perf_counter()
                for pcm in frames:
                    target += .032
                    if self.done.wait(max(0, target - perf_counter())): return
                    self.inbox.push(pcm, perf_counter())
            self.thread = Thread(target=feed, name='qa-speech-replay')
            self.thread.start()
        def close(self):
            self.done.set()
            self.thread.join(2)
    for _ in range(3):
        test_settings = replace(settings, provider='gemini', synthesizer='sapi', sapi_voice=voice,
                                volume=0, history_turns=0, max_tokens=64, max_response_chars=200,
                                timeout_s=15)
        c = VoiceSessionController(test_settings)
        errors, token_at, audio_at = [], [], []
        c.notification.connect(errors.append)
        c.playback.started.connect(audio_at.append)
        record = c.metrics.record
        def timed_record(stage, seconds):
            if stage == 'llm_first': token_at.append(perf_counter())
            record(stage, seconds)
        c.metrics.record = timed_record
        original = c.disfluency_filter.clean
        def clean(raw):
            started = perf_counter()
            output = original(raw)
            record('disfluency_filter', perf_counter() - started)
            return output
        c.disfluency_filter.clean = clean
        with patch('alice_voice.controller.CaptureWorker',
                   side_effect=lambda *args, **kwargs: CaptureWorker(*args, **kwargs, capture_factory=ReplayCapture)):
            c.enable_microphone()
            completed = pump(lambda: audio_at or errors, 35)
        row = {'source': 'real-time replay of synthetic Russian speech; real Silero/Vosk/Gemini/SAPI/QAudioSink',
               'synthesizer_override': 'sapi (configured ElevenLabs has no key)',
               'recognized': c.recognized, 'audio_active': bool(audio_at),
               'metrics': c.metrics.summary(), 'errors': errors, 'completed': completed}
        if audio_at and token_at:
            row['llm_first_chunk_to_audio_active_ms'] = (audio_at[0] - token_at[0]) * 1000
        c.shutdown()
        row['workers_closed'] = pump(lambda: c.capture is None and c.runtime is None, 8)
        c.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        result.append(row)
        if errors or not completed:
            break
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live-replay', action='store_true')
    args = parser.parse_args()
    app = QCoreApplication([])
    settings = Settings.load('settings.json')
    voice = 'Microsoft Irina Desktop - Russian'
    frames = source_pcm(voice)
    result = {'stt': local_stt(settings, frames), 'native_audio': native_audio(settings, voice),
              'voice_interruptions': voice_interruptions(settings)}
    if args.live_replay:
        result['live_replay_diagnostic_sapi'] = live_replay(settings, frames, voice)
    Path('docs/qa-native-results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
