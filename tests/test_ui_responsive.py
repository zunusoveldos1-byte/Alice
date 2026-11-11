"""Responsive layout and transient animation checks in isolated Qt processes."""
import unittest
import test_main_window


class ResponsiveTests(unittest.TestCase):
    run_ui = test_main_window.MainWindowTests.run_ui

    def test_small_window_keeps_controls_visible_and_sidebar_adapts(self):
        self.run_ui('''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from PyQt6.QtCore import QPoint, Qt
            from PyQt6.QtTest import QTest
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app = QApplication([])
            with TemporaryDirectory() as directory:
                w = MainWindow(VoiceSessionController(Settings()), Path(directory)/'settings.json')
                w.show()
                for width,height in ((480,560),(800,650),(1280,850)):
                    w.resize(width,height)
                    QTest.qWait(120)
                    for button in (w.mic_btn,w.camera_btn,w.more_btn,w.minimize_btn,w.maximize_btn,w.close_btn):
                        rect = button.rect().translated(button.mapTo(w, QPoint(0,0)))
                        assert w.rect().contains(rect), (button.accessibleName(),rect,w.rect())
                    assert w.orb.geometry().bottom() < w.state_label.geometry().top()
                    assert w.sidebar.width() == (76 if width<950 else 290)
                w.resize(480,560)
                w.controller.recognized='Привет, Алиса, помоги спланировать сегодняшний рабочий день.'
                w._notice('Камера недоступна. Проверьте подключение и разрешения Windows.')
                QTest.qWait(160)
                point=w.mic_btn.mapTo(w,w.mic_btn.rect().center())
                assert w.childAt(point) is w.mic_btn, 'a label covers the microphone'
                w.controller.mic_state='error'
                w.controller.changed.emit()
                QTest.qWait(120)
                point=w.mic_btn.mapTo(w,w.mic_btn.rect().center())
                assert w.childAt(point) is w.mic_btn, 'error/retry UI covers the microphone'
                w.controller.mic_state='off'
                w.resize(1280,850)
                QTest.qWait(80)
                w._collapse_sidebar()
                assert w.sidebar.width()==76
                w._collapse_sidebar()
                assert w.sidebar.width()==290
                w.controller.partial='long-'*150
                w.controller.changed.emit()
                QTest.qWait(100)
                assert w.user_label.height()<=44
                w.close()
                QTest.qWait(150)
        ''')

    def test_caption_buttons_dispatch_and_track_native_window_state(self):
        self.run_ui('''
            from pathlib import Path
            from tempfile import TemporaryDirectory
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
                QTest.mouseClick(w.maximize_btn, Qt.MouseButton.LeftButton)
                QTest.qWait(100)
                assert w.isMaximized() and w.maximize_btn.action == 'restore'
                QTest.mouseClick(w.maximize_btn, Qt.MouseButton.LeftButton)
                QTest.qWait(100)
                assert not w.isMaximized() and w.maximize_btn.action == 'maximize'
                QTest.mouseClick(w.minimize_btn, Qt.MouseButton.LeftButton)
                QTest.qWait(100)
                assert w.isMinimized()
                w.showNormal()
                QTest.mouseClick(w.close_btn, Qt.MouseButton.LeftButton)
                QTest.qWait(150)
                assert not w.isVisible()
        ''')

    def test_entrance_effect_is_transient_and_can_be_disabled(self):
        self.run_ui('''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from PyQt6.QtTest import QTest
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app = QApplication([])
            with TemporaryDirectory() as directory:
                w = MainWindow(VoiceSessionController(Settings()), Path(directory)/'settings.json')
                w.show()
                assert w.content.graphicsEffect() is not None
                QTest.qWait(400)
                assert w.content.graphicsEffect() is None
                w._home()
                assert w.content.graphicsEffect() is not None
                w.set_motion_enabled(False)
                assert w.content.graphicsEffect() is None
                w._home()
                assert w.content.graphicsEffect() is None
                w.close()
                QTest.qWait(150)
        ''')

    def test_dialog_fade_finishes_inside_modal_loop_and_header_drag_survives(self):
        self.run_ui('''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from unittest.mock import Mock, patch
            from PyQt6.QtCore import QPoint, Qt, QTimer
            from PyQt6.QtTest import QTest
            from PyQt6.QtWidgets import QApplication, QDialog, QLabel, QVBoxLayout
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app = QApplication([])
            with TemporaryDirectory() as directory:
                w = MainWindow(VoiceSessionController(Settings()), Path(directory)/'settings.json')
                w.show()
                handle = Mock()
                handle.startSystemMove.return_value = False
                with patch.object(w, 'windowHandle', return_value=handle):
                    QTest.mousePress(w, Qt.MouseButton.LeftButton, pos=w.header.mapTo(w, QPoint(8,8)))
                    assert w.drag_offset is not None
                    QTest.mouseRelease(w, Qt.MouseButton.LeftButton)
                    assert w.drag_offset is None
                    handle.startSystemMove.assert_called_once()
                dialog = QDialog(w)
                layout = QVBoxLayout(dialog)
                layout.addWidget(QLabel('Проверка появления окна'))
                observed = []
                def during_fade():
                    effect = dialog.graphicsEffect()
                    observed.append(effect is not None and 0 < effect.opacity() < 1)
                def after_fade():
                    observed.append(dialog.graphicsEffect() is None)
                    dialog.accept()
                QTimer.singleShot(120, during_fade)
                QTimer.singleShot(450, after_fade)
                assert w._exec_dialog(dialog) == QDialog.DialogCode.Accepted
                assert observed == [True, True], observed
                assert not w._dialog_fades
                w.close()
                QTest.qWait(150)
        ''')
