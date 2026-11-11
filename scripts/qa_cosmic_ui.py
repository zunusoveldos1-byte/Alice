"""Render the actual idle Qt interface at supported window sizes; no capture or model starts.
Run with QT_QPA_PLATFORM=offscreen for headless QA. Outputs docs/redesign/*.png.
"""
from pathlib import Path
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest
from alice_voice.config import Settings
from alice_voice.controller import VoiceSessionController
from alice_voice.ui.main_window import MainWindow
app=QApplication([])
from PyQt6.QtGui import QFontDatabase
if not QFontDatabase.families():
    for filename in ('segoeui.ttf','seguisb.ttf'):
        font=Path('C:/Windows/Fonts')/filename
        if font.exists(): QFontDatabase.addApplicationFont(str(font))
c=VoiceSessionController(Settings()); w=MainWindow(c,Path('.build-tmp/preview-settings.json')); w.show(); QTest.qWait(400)
Path('docs/redesign').mkdir(exist_ok=True)
w.grab().save('docs/redesign/alice-voice-desktop.png')
for width,height in ((800,650),(480,560),(1280,720)):
    w.resize(width,height); QTest.qWait(100)
    w.grab().save(f'docs/redesign/alice-voice-{width}x{height}.png')
w.close(); QTest.qWait(100)
