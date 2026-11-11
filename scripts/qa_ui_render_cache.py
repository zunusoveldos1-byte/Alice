"""Profile 120 accelerated Qt frames with fixed content, no audio or providers.

Measures frame-loop work and offscreen painting, not production CPU utilization.
Run before and after the implementation in separate processes.
"""
import argparse
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import perf_counter, process_time
from unittest.mock import patch
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt6.QtGui import QFontMetrics
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
from alice_voice.config import Settings
from alice_voice.controller import VoiceSessionController
from alice_voice.ui.main_window import MainWindow


def measure():
    app=QApplication([])
    with TemporaryDirectory() as directory:
        c=VoiceSessionController(Settings()); c.timer.stop()
        w=MainWindow(c,Path(directory)/'settings.json')
        w.resize(800,700); w.show(); QTest.qWait(150)
        w.render_timer.setInterval(1000000)
        c.recognized='Пожалуйста, помоги составить расписание на завтра. '*10
        c.answer='Хорошо, начнём с самых важных задач.'
        w._render(); app.processEvents(); w._render()
        w.clock.setText('22:22'); w.dirty=False
        initial_phase=w.orb.phase
        with patch('alice_voice.ui.cosmic_shell.QFontMetrics',wraps=QFontMetrics) as metrics, \
             patch.object(w,'_render',wraps=w._render) as render, \
             patch('alice_voice.ui.main_window.QTime.currentTime') as clock:
            clock.return_value.toString.return_value='22:22'
            start, cpu=perf_counter(), process_time()
            for _ in range(120):
                w._tick(); app.processEvents()
            elapsed, used=perf_counter()-start, process_time()-cpu
            result={'frames':120,'wall_ms':elapsed*1000,'process_cpu_ms':used*1000,
                    'full_content_renders':render.call_count,'text_metric_builds':metrics.call_count,
                    'animation_advanced':w.orb.phase!=initial_phase,
                    'microphone_opened':False,'llm_used':False,'qt_platform':app.platformName()}
        w.allow_close=True; w.close(); c.shutdown()
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=measure()
    args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))
