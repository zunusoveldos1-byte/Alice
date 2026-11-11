"""Real offscreen Qt integration, isolated from QCoreApplication unit tests."""
import os
import subprocess
import sys
import textwrap
import unittest


class MainWindowTests(unittest.TestCase):
    def test_enrollment_does_not_enable_optional_owner_verification(self):
        self.run_ui('''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from unittest.mock import patch
            from PyQt6.QtCore import pyqtSignal
            from PyQt6.QtTest import QTest
            from PyQt6.QtWidgets import QApplication, QDialog, QPushButton
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app = QApplication([])
            class Enrollment(QDialog):
                enrolled = pyqtSignal()
                def __init__(self, settings, parent):
                    super().__init__(parent)
                    self.start = QPushButton(self)
            with TemporaryDirectory() as directory:
                c = VoiceSessionController(Settings())
                window = MainWindow(c, Path(directory) / 'settings.json')
                with patch('alice_voice.ui.owner_dialog.OwnerEnrollmentDialog', Enrollment), \
                     patch.object(window, '_exec_dialog', side_effect=lambda dialog: dialog.enrolled.emit()), \
                     patch.object(window, '_request_settings') as save:
                    window._register_owner()
                    save.assert_not_called()
                    assert not c.settings.owner_only
                    assert not window.settings_store.desired.owner_only
                window.close()
                QTest.qWait(150)
        ''')

    def run_ui(self, script: str) -> None:
        result = subprocess.run([sys.executable, '-B', '-c', textwrap.dedent(script)],
                                env=dict(os.environ, QT_QPA_PLATFORM='offscreen'),
                                capture_output=True, text=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_idle_clock_updates_without_backend_changes(self):
        self.run_ui('''
            from tempfile import TemporaryDirectory
            from pathlib import Path
            from unittest.mock import Mock, patch
            from PyQt6.QtTest import QTest
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app = QApplication([])
            with TemporaryDirectory() as directory:
                c = VoiceSessionController(Settings())
                w = MainWindow(c, Path(directory) / 'settings.json')
                w.show()
                QTest.qWait(80)
                time = Mock()
                time.toString.return_value = '23:59'
                with patch('alice_voice.ui.main_window.QTime.currentTime', return_value=time):
                    QTest.qWait(120)
                    assert w.clock.text() == '23:59'
                w.close()
                QTest.qWait(100)
        ''')

    def test_microphone_status_streaming_text_and_gui_thread(self):
        self.run_ui('''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from threading import Thread
            from unittest.mock import Mock
            from PyQt6.QtCore import Qt, QThread
            from PyQt6.QtTest import QTest
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app = QApplication([])
            with TemporaryDirectory() as directory:
                c = VoiceSessionController(Settings())
                window = MainWindow(c, Path(directory) / 'settings.json')
                window.show()
                c.enable_microphone = Mock()
                QTest.mouseClick(window.mic_btn, Qt.MouseButton.LeftButton)
                c.enable_microphone.assert_called_once()
                c.request_screen_preview = Mock()
                QTest.mouseClick(window.screen_btn, Qt.MouseButton.LeftButton)
                c.request_screen_preview.assert_called_once_with()
                capture = Mock()
                capture.stop_event.is_set.return_value = False
                c.capture = capture
                c.disable_microphone = Mock()
                QTest.mouseClick(window.mic_btn, Qt.MouseButton.LeftButton)
                c.disable_microphone.assert_called_once()
                c.capture = None
                c.mic_state = 'listening'
                c.changed.emit()
                QTest.qWait(100)
                assert window.state_label.text() == 'Я слушаю'
                token = c.guard.begin('test')
                c.recognized = 'Привет'
                observed = []
                original = window._render
                def render():
                    observed.append(QThread.currentThread() == app.thread())
                    original()
                window._render = render
                def worker():
                    c.signals.stage.emit(token, 'llm')
                    for i in range(100): c.signals.answer.emit(token, 'Ответ ' + str(i))
                thread = Thread(target=worker)
                thread.start(); thread.join()
                QTest.qWait(220)
                assert window.state_label.text() == 'Думаю'
                assert 'Привет' in window.user_label.text()
                assert window.answer_text.toPlainText() == 'Ответ 99'
                assert observed and all(observed)
                assert len(observed) <= 5, observed
                c.stage = 'playback'
                c.output_started = True
                c.first_voice_recorded = True
                c.changed.emit()
                QTest.qWait(100)
                assert window.state_label.text() == 'Думаю', 'gap between PCM chunks is not active audio'
                c.current_chunk = (0, None, 0)
                c.changed.emit()
                QTest.qWait(100)
                assert window.state_label.text() == 'Отвечаю'
                c.current_chunk = None
                c.capture = None
                c.guard.cancel()
                window.close()
                QTest.qWait(150)
                assert not window.isVisible()
        ''')

    def test_quick_settings_persist_and_failed_save_keeps_backend(self):
        self.run_ui('''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from unittest.mock import patch
            from PyQt6.QtCore import Qt
            from PyQt6.QtTest import QTest
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app = QApplication([])
            def wait_for(predicate):
                for _ in range(150):
                    if predicate(): return
                    QTest.qWait(20)
                raise AssertionError('settings did not settle')
            with TemporaryDirectory() as directory:
                path = Path(directory) / 'settings.json'
                Settings().save(path)
                c = VoiceSessionController(Settings())
                window = MainWindow(c, path)
                window.show()
                window.model_combo.setEditText('custom-local-model')
                window.model_combo.lineEdit().editingFinished.emit()
                wait_for(lambda: c.settings.ollama_model == 'custom-local-model')
                assert Settings.load(path).ollama_model == 'custom-local-model'
                window.voice_combo.setCurrentIndex(window.voice_combo.findData('edge'))
                wait_for(lambda: c.settings.synthesizer == 'edge')
                assert Settings.load(path).synthesizer == 'edge'
                eleven_index = window.voice_combo.findData('elevenlabs')
                assert eleven_index >= 0
                assert window.voice_combo.itemText(eleven_index) == 'ElevenLabs (онлайн)'
                window.voice_combo.setCurrentIndex(eleven_index)
                wait_for(lambda: c.settings.synthesizer == 'elevenlabs')
                assert Settings.load(path).synthesizer == 'elevenlabs'
                assert c.capture is None, 'changing settings unexpectedly opened microphone'
                with patch.object(Settings, 'save', side_effect=OSError('private-value')):
                    window.model_combo.setEditText('will-not-save')
                    window.model_combo.lineEdit().editingFinished.emit()
                    wait_for(lambda: not window.settings_store.busy)
                    QTest.qWait(100)
                assert c.settings.ollama_model == 'custom-local-model'
                assert Settings.load(path).ollama_model == 'custom-local-model'
                assert 'private-value' not in window.notice.text()
                assert window.model_combo.currentText() == 'custom-local-model'
                window.close()
                QTest.qWait(150)
                assert not window.isVisible()
        ''')

    def test_close_waits_for_coalesced_saves_without_blocking_gui(self):
        self.run_ui('''
            from dataclasses import replace
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from threading import Event
            from unittest.mock import patch
            from PyQt6.QtCore import QTimer
            from PyQt6.QtTest import QTest
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app = QApplication([])
            entered, release = Event(), Event()
            original = Settings.save
            written, ticks = [], []
            def slow_save(settings, path):
                written.append(settings.ollama_model)
                if settings.ollama_model == 'first':
                    entered.set()
                    assert release.wait(5)
                original(settings, path)
            with TemporaryDirectory() as directory:
                path = Path(directory) / 'settings.json'
                c = VoiceSessionController(Settings())
                window = MainWindow(c, path)
                window.show()
                timer = QTimer()
                timer.setInterval(10)
                timer.timeout.connect(lambda: ticks.append(1))
                timer.start()
                with patch.object(Settings, 'save', slow_save):
                    window.settings_store.request(replace(c.settings, ollama_model='first'))
                    for _ in range(50):
                        if entered.is_set(): break
                        QTest.qWait(10)
                    assert entered.is_set()
                    window.settings_store.request(replace(c.settings, ollama_model='intermediate'))
                    window.settings_store.request(replace(c.settings, ollama_model='latest'))
                    window.close()
                    # Offscreen first paint can consume over 100 ms on Windows.
                    # Require event dispatch while the writer is still held,
                    # without assuming two timer deliveries in that first paint.
                    initial_ticks = len(ticks)
                    for _ in range(100):
                        if len(ticks) >= initial_ticks + 2: break
                        QTest.qWait(10)
                    assert window.isVisible(), 'window closed before authorized writes finished'
                    assert len(ticks) >= initial_ticks + 2, 'GUI blocked while waiting for disk'
                    release.set()
                    for _ in range(150):
                        if not window.isVisible(): break
                        QTest.qWait(20)
                assert not window.isVisible()
                assert written == ['first', 'latest'], written
                assert Settings.load(path).ollama_model == 'latest'
                assert c.capture is None
        ''')
