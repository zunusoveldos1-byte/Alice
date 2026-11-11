"""Capture real native windows after exercising the repaired shell. No audio starts."""
from pathlib import Path
import json
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
from alice_voice.config import Settings
from alice_voice.controller import VoiceSessionController
from alice_voice.ui.main_window import MainWindow

app = QApplication([])
controller = VoiceSessionController(Settings.load('settings.json'))
window = MainWindow(controller, 'settings.json')
window.showFullScreen()
QTest.qWait(450)
window.settings_btn.click()
QTest.qWait(250)
page = window.settings_page
assert window.pages.currentWidget() is page and not page.isWindow()
Path('docs/redesign').mkdir(exist_ok=True)
window.grab().save('docs/redesign/settings-page-native.png')
page.categories.setCurrentRow(1)
QTest.qWait(120)
window.grab().save('docs/redesign/settings-models-native.png')
window.nav_buttons['mini'].click()
QTest.qWait(200)
mini = window.mini
assert mini.isVisible() and mini.controller is controller
before = mini.pos()
QTest.mousePress(mini.drag_handle, Qt.MouseButton.LeftButton, pos=QPoint(20,14))
QTest.mouseMove(mini.drag_handle, QPoint(70,44))
QTest.mouseRelease(mini.drag_handle, Qt.MouseButton.LeftButton, pos=QPoint(70,44))
assert before != mini.pos()
window._open_mini()
assert window.mini is mini
mini.return_button.click()
assert window.isVisible() and not mini.isVisible()
window._open_mini()
mini.close()
assert not controller.closing and window.isVisible()
window._open_mini()
QTest.qWait(150)
mini.grab().save('docs/redesign/mini-assistant-native.png')
report = {'embedded_settings': True, 'mini_visible': mini.isVisible(),
          'dragged': before != mini.pos(), 'single_instance': window.mini is mini,
          'shared_controller': mini.controller is controller,
          'clock': window.clock.text(), 'dpr': window.devicePixelRatioF()}
Path('docs/redesign/shell-native-check.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
app.exec()
