"""Exercise motion in a real widget event loop without starting audio services."""
import unittest
import test_main_window


class MotionTests(unittest.TestCase):
    run_ui = test_main_window.MainWindowTests.run_ui

    def test_background_repaints_motion_theme_resize_and_transparent_corners(self):
        self.run_ui('''
            from PyQt6.QtCore import Qt
            from PyQt6.QtWidgets import QApplication
            from alice_voice.ui.widgets import GlassBackground
            app = QApplication([])
            background = GlassBackground()
            background.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            background.resize(400, 300)
            background.show()
            app.processEvents()
            initial = background.grab().toImage()
            background.phase = 1.0
            moved = background.grab().toImage()
            assert moved != initial
            assert moved.pixelColor(0, 0).alpha() == 0
            background.light = True
            light = background.grab().toImage()
            assert light.pixelColor(200, 60).lightness() > moved.pixelColor(200, 60).lightness()
            background.resize(600, 350)
            app.processEvents()
            resized = background.grab().toImage()
            assert resized.width() > light.width()
            assert resized.pixelColor(resized.width()//2, resized.height()//2).alpha() == 255
            assert resized.pixelColor(0, 0).alpha() == 0
            background.close()
        ''')

    def test_visible_background_and_hidden_static_frames(self):
        self.run_ui('''
            from tempfile import TemporaryDirectory
            from pathlib import Path
            from PyQt6.QtCore import Qt
            from PyQt6.QtTest import QTest
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app = QApplication([])
            with TemporaryDirectory() as directory:
                w = MainWindow(VoiceSessionController(Settings()), Path(directory)/'settings.json')
                w.show()
                w.resize(w.minimumSize())
                QTest.qWait(80)
                mic_bottom = w.mic_btn.mapTo(w.background, w.mic_btn.rect().bottomLeft()).y()
                state_top = w.state_label.mapTo(w.background, w.state_label.rect().topLeft()).y()
                assert state_top < mic_bottom, (mic_bottom, state_top)
                phase = w.background.phase
                QTest.qWait(220)
                assert w.background.phase > phase
                assert w.mic_btn.width() == w.mic_btn.height() == 52
                assert w.orb.width() <= 340
                assert w.windowFlags() & Qt.WindowType.FramelessWindowHint
                assert w.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
                w.hide()
                phase = w.background.phase
                QTest.qWait(180)
                assert w.background.phase == phase
                w.showMinimized()
                QTest.qWait(80)
                phase = w.background.phase
                QTest.qWait(180)
                assert w.background.phase == phase
                w.showNormal()
                w.show()
                w.set_motion_enabled(False)
                QTest.qWait(80)
                phase = w.background.phase
                w.controller.mic_state = 'listening'
                w.controller.changed.emit()
                QTest.qWait(180)
                assert w.background.phase == phase
                assert w.state_label.text() == 'Я слушаю'
                assert w.state_label.progress == 1
                w.close()
                QTest.qWait(150)
        ''')

    def test_status_crossfade_latest_wins_and_repeated_target_does_not_restart(self):
        self.run_ui('''
            from PyQt6.QtWidgets import QApplication
            from alice_voice.ui.widgets import StatusLabel
            app = QApplication([])
            label = StatusLabel()
            label.resize(400, 44)
            label.set_status('Готов', 1.0)
            assert label.progress == 1
            label.set_status('Слушаю вас...', 2.0)
            label.advance(2.1)
            assert 0 < label.progress < 1
            progress = label.progress
            label.set_status('Слушаю вас...', 2.1)
            assert label.progress == progress
            visible = label._snapshot().toImage()
            label.set_status('Думаю...', 2.11)
            assert label._previous_frame.toImage() == visible
            assert label.text() == 'Думаю...'
            label.advance(2.4)
            assert label.progress == 1
            label.set_status('Говорю...', 2.5, animate=False)
            assert label.progress == 1
            assert label.text() == 'Говорю...'
        ''')
