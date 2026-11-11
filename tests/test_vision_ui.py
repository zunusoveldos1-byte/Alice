"""Isolated Qt tests for preview, area selection and explicit sending."""
import os
import subprocess
import sys
import textwrap
import unittest


class VisionUITests(unittest.TestCase):
    def run_ui(self, source):
        result = subprocess.run([sys.executable, '-B', '-c', textwrap.dedent(source)],
                                env=dict(os.environ, QT_QPA_PLATFORM='offscreen'),
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_preview_requires_capture_and_releases_images_on_cancel(self):
        self.run_ui('''
            from io import BytesIO
            from unittest.mock import patch
            from PIL import Image
            from PyQt6.QtWidgets import QApplication
            from alice_voice.ui.vision_dialog import ScreenVisionDialog
            app = QApplication([])
            with Image.new('RGB', (200, 100), 'blue') as image, BytesIO() as buffer:
                image.save(buffer, format='JPEG')
                data = buffer.getvalue()
            with patch('alice_voice.ui.vision_dialog.ScreenWorker.start'):
                dialog = ScreenVisionDialog('Объясни ошибку')
                assert not dialog.send_btn.isEnabled()
                dialog._captured(data, {'left': 0, 'top': 0, 'width': 200, 'height': 100})
                assert dialog.send_btn.isEnabled()
                assert dialog.image_jpeg == data
                dialog.reject()
                assert dialog.image_jpeg is None
                assert dialog.preview.pixmap().isNull()
        ''')

    def test_capture_and_crop_run_off_gui_and_preserve_physical_coordinates(self):
        self.run_ui('''
            from io import BytesIO
            from threading import get_ident
            from time import perf_counter
            from unittest.mock import patch
            from PIL import Image
            from PyQt6.QtCore import QPoint, Qt
            from PyQt6.QtTest import QTest
            from PyQt6.QtWidgets import QApplication
            from alice_voice.ui.vision_dialog import ScreenVisionDialog
            app = QApplication([])
            monitor = dict(index=1, left=-1920, top=0, width=1920, height=1080, is_primary=True)
            calls, threads = [], []
            with Image.new('RGB', (640, 360), 'blue') as image, BytesIO() as buffer:
                image.save(buffer, format='JPEG')
                data = buffer.getvalue()
            def capture(*, region):
                calls.append(region)
                threads.append(get_ident())
                return data
            def wait_for(predicate):
                deadline = perf_counter() + 3
                while not predicate() and perf_counter() < deadline:
                    QTest.qWait(10)
                assert predicate()
            with patch('alice_voice.ui.vision_dialog.list_capture_monitors', return_value=[monitor]), \
                 patch('alice_voice.ui.vision_dialog.capture_screen_jpeg', side_effect=capture):
                d = ScreenVisionDialog('Что здесь?')
                d.show()
                wait_for(lambda: d.monitor_combo.count() == 1)
                d.mode_combo.setCurrentIndex(d.mode_combo.findData('region'))
                d.capture_btn.click()
                wait_for(lambda: d.image_jpeg is not None)
                assert not d.send_btn.isEnabled(), 'full-monitor preview sent as selected region'
                rect = d.preview._image_rect()
                start = QPoint(rect.x() + rect.width() // 4, rect.y() + rect.height() // 4)
                end = QPoint(rect.x() + rect.width() * 3 // 4, rect.y() + rect.height() * 3 // 4)
                QTest.mousePress(d.preview, Qt.MouseButton.LeftButton, pos=start)
                QTest.mouseMove(d.preview, end)
                QTest.mouseRelease(d.preview, Qt.MouseButton.LeftButton, pos=end)
                wait_for(lambda: d.image_jpeg is not None and d.send_btn.isEnabled())
                assert len(calls) == 2
                assert -1450 <= calls[-1]['left'] <= -1430, calls
                assert 950 <= calls[-1]['width'] <= 970, calls
                assert all(thread != get_ident() for thread in threads)
                d.reject()
                assert d.image_jpeg is None
        ''')

    def test_worker_start_failure_is_visible_and_cannot_send(self):
        self.run_ui('''
            from unittest.mock import patch
            from PyQt6.QtWidgets import QApplication
            from alice_voice.ui.vision_dialog import ScreenVisionDialog
            app = QApplication([])
            with patch('alice_voice.ui.vision_dialog.ScreenWorker.start', side_effect=OSError('private-value')):
                dialog = ScreenVisionDialog('Что здесь?')
                assert not dialog.send_btn.isEnabled()
                assert 'private-value' not in dialog.notice.text()
                assert 'Не удалось' in dialog.notice.text()
                dialog.reject()
        ''')

    def test_active_window_delay_and_late_result_after_cancel(self):
        self.run_ui('''
            from io import BytesIO
            from threading import Event
            from time import perf_counter
            from unittest.mock import patch
            from PIL import Image
            from PyQt6.QtTest import QTest
            from PyQt6.QtWidgets import QApplication
            from alice_voice.ui.vision_dialog import ScreenVisionDialog
            app = QApplication([])
            entered, release = Event(), Event()
            region = dict(left=-100, top=50, width=200, height=100)
            with Image.new('RGB', (200, 100), 'blue') as image, BytesIO() as buffer:
                image.save(buffer, format='JPEG')
                data = buffer.getvalue()
            def capture(**options):
                assert options['region'] == region
                entered.set()
                release.wait(3)
                return data
            with patch('alice_voice.ui.vision_dialog.list_capture_monitors', return_value=[]), \
                 patch('alice_voice.ui.vision_dialog.active_window_region', return_value=region), \
                 patch('alice_voice.ui.vision_dialog.capture_screen_jpeg', side_effect=capture):
                d = ScreenVisionDialog('Что здесь?')
                d.show()
                d.mode_combo.setCurrentIndex(d.mode_combo.findData('window'))
                d.capture_btn.click()
                assert d.capture_timer.interval() == 3000
                d.capture_timer.start(10)
                deadline = perf_counter() + 2
                while not entered.is_set() and perf_counter() < deadline:
                    QTest.qWait(10)
                try:
                    assert entered.is_set()
                    worker = d.worker
                    d.reject()
                    release.set()
                    deadline = perf_counter() + 2
                    while worker.is_alive() and perf_counter() < deadline:
                        QTest.qWait(10)
                    QTest.qWait(30)
                    assert not worker.is_alive()
                    assert d.image_jpeg is None
                    assert not d.capture_timer.isActive()
                    assert not d.isVisible()
                finally:
                    release.set()
        ''')

    def test_main_window_sends_exact_preview_and_edited_prompt(self):
        self.run_ui('''
            from io import BytesIO
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from unittest.mock import Mock, patch
            from PIL import Image
            from PyQt6.QtCore import QTimer
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            from alice_voice.ui.vision_dialog import ScreenVisionDialog
            app = QApplication([])
            with Image.new('RGB', (200, 100), 'blue') as image, BytesIO() as buffer:
                image.save(buffer, format='JPEG')
                data = buffer.getvalue()
            with TemporaryDirectory() as directory, patch('alice_voice.ui.vision_dialog.ScreenWorker.start'):
                c = VoiceSessionController(Settings(provider='gemini'))
                c.request_screen = Mock()
                w = MainWindow(c, Path(directory) / 'settings.json')
                w.show()
                def accept_preview():
                    dialog = app.activeModalWidget()
                    assert isinstance(dialog, ScreenVisionDialog)
                    c.request_screen.assert_not_called()
                    dialog._captured(data, {'left': 0, 'top': 0, 'width': 200, 'height': 100})
                    dialog.prompt.setPlainText('Объясни выделенную ошибку')
                    dialog.accept()
                QTimer.singleShot(50, accept_preview)
                w._show_screen_preview('Что на экране?', 'voice-id')
                c.request_screen.assert_called_once_with('Объясни выделенную ошибку',
                                                        utterance_id='voice-id', image_jpeg=data)
                assert not c.screen_preview_active
                w.close()
                app.processEvents()
        ''')
