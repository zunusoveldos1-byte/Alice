"""Window mode and responsive geometry regressions for the native shell."""
import unittest
import test_main_window


class CosmicRefinementTests(unittest.TestCase):
    run_ui = test_main_window.MainWindowTests.run_ui

    def test_fullscreen_exit_restore_and_silent_orb(self):
        self.run_ui('''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from PyQt6.QtCore import Qt, QPoint
            from PyQt6.QtTest import QTest
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app = QApplication([])
            with TemporaryDirectory() as directory:
                c = VoiceSessionController(Settings())
                w = MainWindow(c, Path(directory)/'settings.json')
                w.showFullScreen()
                QTest.qWait(100)
                assert w.isFullScreen()
                assert w.maximize_btn.action == 'restore'
                assert not w.size_grip.isVisible()
                QTest.keyClick(w, Qt.Key.Key_Escape)
                QTest.qWait(60)
                assert not w.isFullScreen()
                QTest.keyClick(w, Qt.Key.Key_F11)
                QTest.qWait(60)
                assert w.isFullScreen()
                QTest.mouseClick(w.maximize_btn, Qt.MouseButton.LeftButton)
                QTest.qWait(60)
                assert not w.isFullScreen() and not w.isMaximized()
                c.level = 1.0
                w._render()
                assert w.orb.level == 0 and w.wave.level == 0
                for width, height in ((480,560),(800,650),(1280,720),(1920,1080)):
                    w.resize(width,height)
                    QTest.qWait(80)
                    for widget in (w.mic_btn,w.camera_btn,w.more_btn,w.clock,w.settings_icon):
                        rect = widget.rect().translated(widget.mapTo(w,QPoint()))
                        assert w.rect().contains(rect), (width,height,rect)
                w.close()
                QTest.qWait(150)
        ''')
