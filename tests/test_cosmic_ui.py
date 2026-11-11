import os
import subprocess
import sys
import textwrap
import unittest


class CosmicUITests(unittest.TestCase):
    def test_navigation_preserves_session_and_silence(self):
        script = '''
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest
from alice_voice.config import Settings
from alice_voice.controller import VoiceSessionController
from alice_voice.ui.main_window import MainWindow
app = QApplication([])
with TemporaryDirectory(dir='.build-tmp', ignore_cleanup_errors=True) as directory:
    c = VoiceSessionController(Settings())
    w = MainWindow(c, Path(directory) / 'settings.json')
    w.show()
    c.completed_turns.append(('Привет', 'Здравствуйте'))
    runtime = c.runtime
    c.enable_microphone = Mock()
    c.disable_microphone = Mock()
    for page in ('work', 'dialog', 'voice'):
        w.select_page(page)
        QTest.qWait(60)
        assert w.current_page == page
    assert c.runtime is runtime
    assert list(c.completed_turns) == [('Привет', 'Здравствуйте')]
    c.enable_microphone.assert_not_called()
    c.disable_microphone.assert_not_called()
    c.level = 0
    w._render()
    assert w.orb.level == 0
    assert not w.user_label.isVisible()
    assert w.camera is None
    w._open_mini()
    assert w.mini.controller is c
    w.mini.close()
    w.close()
    QTest.qWait(100)
'''
        result = subprocess.run([sys.executable, '-B', '-c', textwrap.dedent(script)],
            env=dict(os.environ, QT_QPA_PLATFORM='offscreen'), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_volume_focus_delay_and_persistence(self):
        from test_main_window import MainWindowTests
        MainWindowTests.run_ui(self, '''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from PyQt6.QtCore import Qt, QPoint
            from PyQt6.QtTest import QTest
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app=QApplication([])
            with TemporaryDirectory() as directory:
                path=Path(directory)/'settings.json'
                w=MainWindow(VoiceSessionController(Settings()),path); w.show()
                QTest.qWait(100)
                v=w.volume_control
                v.button.setFocus(Qt.FocusReason.TabFocusReason)
                QTest.qWait(160)
                assert v.panel.isVisible()
                v.slider.setFocus(Qt.FocusReason.TabFocusReason)
                v._schedule_hide(); QTest.qWait(300)
                assert v.panel.isVisible(), 'keyboard traversal closed volume popup'
                v.slider.setValue(37)
                for _ in range(100):
                    if path.exists() and not w.settings_store.busy: break
                    QTest.qWait(20)
                assert Settings.load(path).volume==.37
                assert v.label.text()=='37%'
                w.mic_btn.setFocus(); QTest.mouseMove(w,QPoint(400,100)); v._schedule_hide()
                QTest.qWait(450)
                assert not v.panel.isVisible()
                w.close(); QTest.qWait(150)
        ''')

    def test_camera_start_release_and_unavailable(self):
        from test_main_window import MainWindowTests
        MainWindowTests.run_ui(self, '''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from unittest.mock import Mock,patch
            from PyQt6.QtCore import QObject,pyqtSignal
            from PyQt6.QtTest import QTest
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            class Camera(QObject):
                activeChanged=pyqtSignal(bool)
                errorOccurred=pyqtSignal()
                def __init__(self,*args):
                    super().__init__(); self.start=Mock(); self.stop=Mock(); self.deleteLater=Mock()
            app=QApplication([])
            with TemporaryDirectory() as directory:
                w=MainWindow(VoiceSessionController(Settings()),Path(directory)/'settings.json'); w.show()
                with patch('alice_voice.ui.cosmic_shell.QMediaDevices.videoInputs',return_value=[]):
                    w._toggle_camera()
                    assert w.camera is None and 'Камера недоступна' in w.notice_text
                with patch('alice_voice.ui.cosmic_shell.QMediaDevices.videoInputs',return_value=[object()]), patch('alice_voice.ui.cosmic_shell.QCamera',Camera), patch('alice_voice.ui.cosmic_shell.QMediaCaptureSession') as sessions:
                    w._toggle_camera(); camera=w.camera
                    camera.start.assert_called_once()
                    camera.activeChanged.emit(True)
                    assert w.camera_btn.active
                    w._toggle_camera()
                    camera.stop.assert_called_once(); camera.deleteLater.assert_called_once()
                    assert w.camera is None and not w.camera_btn.active
                    sessions.return_value.setCamera.assert_called_with(None)
                    w._toggle_camera(); w.camera.errorOccurred.emit()
                    assert w.camera is None and 'Камера недоступна' in w.notice_text
                w.close(); QTest.qWait(150)
        ''')

    def test_playback_level_tracks_pcm_position_and_pause(self):
        from test_main_window import MainWindowTests
        MainWindowTests.run_ui(self, '''
            from collections import deque
            from unittest.mock import Mock
            import struct
            from PyQt6.QtCore import QCoreApplication
            from alice_voice.playback import PCMPlayback
            app=QCoreApplication([])
            output=PCMPlayback()
            assert output.output_level()==0
            output.sink=Mock(); output.sink.volume.return_value=1.0; output._format_key=(b'device',1000,1)
            output.sink.processedUSecs.return_value=0
            output._packets=deque([{'start':0,'end':80,'data':struct.pack('<40h',*([0]*20+[4096]*20))}])
            assert output.output_level()==0
            output.sink.processedUSecs.return_value=20000
            assert abs(output.output_level()-.5)<.001
            output.sink.volume.return_value=0
            assert output.output_level()==0
            output.sink.volume.return_value=1
            output.paused=True
            assert output.output_level()==0
            output.sink=None; output._packets.clear()
        ''')

    def test_more_menu_dispatches_and_model_errors_surface(self):
        from test_main_window import MainWindowTests
        MainWindowTests.run_ui(self, '''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from unittest.mock import Mock
            from PyQt6.QtCore import QTimer
            from PyQt6.QtTest import QTest
            from PyQt6.QtWidgets import QApplication,QMenu
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app=QApplication([])
            with TemporaryDirectory() as directory:
                c=VoiceSessionController(Settings()); w=MainWindow(c,Path(directory)/'settings.json'); w.show()
                def pick(label):
                    def choose():
                        menu=app.activePopupWidget()
                        assert isinstance(menu,QMenu)
                        action=next(a for a in menu.actions() if a.text()==label)
                        action.trigger(); menu.close()
                    QTimer.singleShot(30,choose)
                    w._open_more()
                pick('Открыть расшифровку')
                assert w.current_page=='dialog'
                c.request_screen_preview=Mock()
                pick('Посмотреть экран')
                c.request_screen_preview.assert_called_once_with()
                token=c.guard.begin('failed-model')
                c._failed(token,'Сервер модели недоступен')
                QTest.qWait(100)
                assert w._mode()[0]=='error'
                assert 'Сервер модели недоступен' in w.notice_banner.text()
                c._failed(token,'Устаревшая ошибка')
                assert c.last_error=='Сервер модели недоступен'
                w.close(); QTest.qWait(150)
        ''')

    def test_keyboard_focus_is_visible_and_mouse_clears_it(self):
        from test_main_window import MainWindowTests
        MainWindowTests.run_ui(self, '''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from PyQt6.QtCore import Qt,QPoint
            from PyQt6.QtTest import QTest
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app=QApplication([])
            with TemporaryDirectory() as directory:
                w=MainWindow(VoiceSessionController(Settings()),Path(directory)/'settings.json'); w.show()
                QTest.qWait(100)
                w.work_mode.setFocus(Qt.FocusReason.TabFocusReason)
                QTest.qWait(40)
                assert w.work_mode.property('keyboardFocus')
                keyboard=w.work_mode.grab().toImage()
                QTest.mouseClick(w.work_mode,Qt.MouseButton.LeftButton)
                QTest.qWait(40)
                assert not w.work_mode.property('keyboardFocus')
                w.collapse_btn.setFocus(Qt.FocusReason.BacktabFocusReason)
                QTest.qWait(40)
                assert w.collapse_btn.property('keyboardFocus')
                w.select_page('voice')
                w.close(); QTest.qWait(150)
        ''')
