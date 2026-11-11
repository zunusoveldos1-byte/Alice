"""Real Qt contracts for content invalidation and independent animation frames."""
import unittest
import test_main_window


class RenderCacheTests(unittest.TestCase):
    run_ui = test_main_window.MainWindowTests.run_ui

    def test_idle_animation_does_not_rebuild_static_content(self):
        self.run_ui('''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from unittest.mock import patch
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app=QApplication([])
            with TemporaryDirectory() as directory:
                c=VoiceSessionController(Settings()); c.timer.stop()
                w=MainWindow(c,Path(directory)/'settings.json'); w.show(); app.processEvents()
                w.clock.setText('22:22'); w.dirty=False
                phase=w.orb.phase
                with patch('alice_voice.ui.main_window.QTime.currentTime') as clock, \
                     patch.object(w,'_render',wraps=w._render) as render:
                    clock.return_value.toString.return_value='22:22'
                    for _ in range(5): w._tick()
                    assert render.call_count==0, render.call_count
                assert w.orb.phase!=phase
                w.allow_close=True; w.close(); c.shutdown()
        ''')

    def test_replica_fit_reuses_metrics_until_text_width_or_font_changes(self):
        self.run_ui('''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from unittest.mock import patch
            from PyQt6.QtGui import QFontMetrics
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app=QApplication([])
            with TemporaryDirectory() as directory:
                c=VoiceSessionController(Settings()); c.timer.stop()
                w=MainWindow(c,Path(directory)/'settings.json'); w.show(); app.processEvents()
                c.recognized='Пожалуйста, помоги составить расписание на завтра. '*10
                w._render(); app.processEvents(); w._render()
                before=w.user_label.text()
                with patch('alice_voice.ui.cosmic_shell.QFontMetrics',wraps=QFontMetrics) as metrics:
                    for _ in range(5): w._render()
                    assert metrics.call_count==0, metrics.call_count
                    assert w.user_label.text()==before
                    c.recognized='Новая реплика с другим текстом.'
                    w._render()
                    assert metrics.call_count==1
                    assert 'Новая реплика' in w.user_label.text()
                    w.user_label.setFixedWidth(w.user_label.width()-40)
                    w._render()
                    assert metrics.call_count==2
                    w.user_label.setStyleSheet('font-size: 22px;'); w._render()
                    assert metrics.call_count==3, metrics.call_count
                    c.recognized=''; w._render()
                    assert w.user_label.text()==''
                w.allow_close=True; w.close(); c.shutdown()
        ''')

    def test_levels_and_smoothing_continue_without_content_changes(self):
        self.run_ui('''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from types import SimpleNamespace
            from unittest.mock import Mock,patch
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app=QApplication([])
            with TemporaryDirectory() as directory:
                c=VoiceSessionController(Settings()); c.timer.stop()
                w=MainWindow(c,Path(directory)/'settings.json'); w.show(); app.processEvents()
                c.capture=SimpleNamespace(); c.mic_state='listening'; c.level=0
                w._render(); w.clock.setText('22:22'); w.dirty=False
                with patch('alice_voice.ui.main_window.QTime.currentTime') as clock:
                    clock.return_value.toString.return_value='22:22'
                    c.level=.1; w._tick()
                    first=w.wave.level
                    w._tick(); assert w.wave.level>first>0
                    w.set_motion_enabled(False); w._tick()
                    c.level=0; w._tick(); assert w.wave.level>0
                    assert w.orb.phase==0
                    c.stage='playback'; c.current_chunk=(0,None,0)
                    c.playback.first_active=False
                    c.playback.output_level=Mock(return_value=.8)
                    w._tick(); c.playback.output_level.assert_called_once()
                    assert w.wave.mode=='speaking'
                c.capture=None; c.current_chunk=None
                w.allow_close=True; w.close(); c.shutdown()
        ''')
