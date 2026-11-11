"""Muted native PCM queue/reuse probe; no acoustic measurement or cloud calls."""
import json
from pathlib import Path
import sys
from time import perf_counter, sleep

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtMultimedia import QAudio
from alice_voice.playback import PCMPlayback
from alice_voice.speech import PCM


def main():
    app = QCoreApplication([])
    playback = PCMPlayback()
    pcm = PCM(bytes(4800), 24000)  # 100 ms PCM16 mono, intentionally silent.
    starts, finishes, errors = [], [], []
    playback.started.connect(starts.append)
    playback.finished.connect(lambda: finishes.append(perf_counter()))
    playback.error.connect(errors.append)
    def pump(predicate, timeout=5):
        deadline = perf_counter() + timeout
        while not predicate() and perf_counter() < deadline:
            app.processEvents()
            sleep(.001)
        return bool(predicate())
    begin = perf_counter()
    playback.play(pcm, volume=0)
    sink = playback.sink
    for _ in range(19):
        if not playback.append(pcm):
            raise RuntimeError('Native append rejected matching PCM')
    drained = pump(lambda: len(finishes) == 20 or errors)
    elapsed = (perf_counter() - begin) * 1000
    playback.stop()
    playback.play(pcm, volume=0)
    reused = playback.sink is sink
    playback.append(pcm)
    playback.stop()
    playback.stop()
    result = {'native_backend': True, 'output_volume': 0,
              'acoustic_measurement': False, 'errors': errors,
              'drained': drained and len(finishes) == 20,
              'packet_starts': len(starts), 'packet_finishes': len(finishes),
              'two_seconds_pcm_to_drain_ms': elapsed, 'same_sink_after_reset': reused,
              'sink_stopped': sink.state() == QAudio.State.StoppedState,
              'buffer_cleared': playback.buffer is None and playback.storage is None,
              'late_append_rejected': not playback.append(pcm)}
    playback.close()
    Path('docs/stage4-pcm-output-results.json').write_text(
        json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result))
    if not result['drained'] or errors or not reused or not result['sink_stopped']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
