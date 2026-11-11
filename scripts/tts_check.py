"""Explicit native SAPI/Qt output check. No microphone and no audio files."""
import json
from pathlib import Path
from queue import Queue, Empty
import sys
from threading import Event, Thread
from time import perf_counter
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt6.QtCore import QCoreApplication, QTimer
from alice_voice.metrics import LatencyMetrics
from alice_voice.playback import PCMPlayback
from alice_voice.speech import sapi_synthesize


def main():
    app = QCoreApplication([])
    playback = PCMPlayback()
    metrics = LatencyMetrics()
    results = Queue(maxsize=1)
    cancel, advance = Event(), Event()
    state = {'finished': 0, 'error': '', 'ready': 0, 'pause_position_preserved': False}
    def synthesize():
        try:
            for _ in range(3):
                start = perf_counter()
                pcm = sapi_synthesize('Проверка голосового ответа.', 'Microsoft Irina Desktop - Russian', cancel, 15)
                metrics.record('tts', perf_counter() - start)
                results.put((pcm, perf_counter()))
                advance.wait(15)
                advance.clear()
                if cancel.is_set(): return
        except BaseException:
            state['error'] = 'SAPI synthesis failed.'
    worker = Thread(target=synthesize, name='sapi-check')
    worker.start()
    def finished():
        state['finished'] += 1
        advance.set()
        if state['finished'] == 3:
            cancel.set()
    def error(message):
        state['error'] = message
        cancel.set()
        advance.set()
    def started(when):
        metrics.record('pcm_to_playback', when - state['ready'])
        if state['finished'] == 0:
            # ActiveState can fire reentrantly inside sink.start(). UI actions
            # occur after it returns, so exercise pause on the next event turn.
            def pause():
                playback.pause()
                position = playback.buffer.pos()
                def resume():
                    state['pause_position_preserved'] = playback.buffer is not None and playback.buffer.pos() == position
                    playback.resume()
                QTimer.singleShot(150, resume)
            QTimer.singleShot(50, pause)
    playback.started.connect(started)
    playback.finished.connect(finished)
    playback.error.connect(error)
    def poll():
        try:
            pcm, ready = results.get_nowait()
            state['ready'] = ready
            state['pcm_rate'] = pcm.sample_rate
            state['pcm_bytes'] = len(pcm.data)
            # Zero volume verifies device/position without disturbing the user.
            playback.play(pcm, volume=0)
        except Empty:
            pass
        except RuntimeError as exc:
            error(str(exc))
        if state['error'] or (state['finished'] == 3 and not worker.is_alive()):
            playback.stop()
            app.quit()
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(poll)
    timer.start()
    def timeout():
        error('Native output check timeout.')
    QTimer.singleShot(30000, timeout)
    app.exec()
    cancel.set(); advance.set()
    worker.join(timeout=3)  # console diagnostic only, GUI already closed
    state['thread_closed'] = not worker.is_alive()
    state.pop('ready')
    state['metrics'] = metrics.summary()
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0 if (state['finished'] == 3 and not state['error']
                 and state['thread_closed'] and state['pause_position_preserved']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
