"""Independent floating surface; the main window owns the only voice session."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout, QHBoxLayout, QPushButton
from .cosmic_widgets import CosmicOrb, IconButton
from .cosmic_theme import STYLE


class MiniAssistant(QWidget):
    def __init__(self, owner):
        # No native owner: minimizing a fullscreen main window must not hide us.
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint |
                         Qt.WindowType.FramelessWindowHint)
        self.controller = owner.controller
        self.setWindowTitle('Alice Voice · Мини-ассистент')
        self.setObjectName('miniAssistant')
        self.setStyleSheet(STYLE + '\nQWidget#miniAssistant { background: #08172A; border: 1px solid #315477; border-radius: 18px; }')
        self.resize(288, 370)
        self._drag_offset = None
        layout = QVBoxLayout(self)
        self.drag_handle = QLabel('Alice Voice   ·   потяните для перемещения')
        self.drag_handle.setFixedHeight(32)
        self.drag_handle.setCursor(Qt.CursorShape.SizeAllCursor)
        self.drag_handle.mousePressEvent = self._drag_press
        self.drag_handle.mouseMoveEvent = self._drag_move
        self.drag_handle.mouseReleaseEvent = self._drag_release
        layout.addWidget(self.drag_handle)
        self.orb = CosmicOrb(); self.orb.setFixedSize(190, 190)
        layout.addWidget(self.orb, alignment=Qt.AlignmentFlag.AlignCenter)
        self.label = QLabel(); self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setWordWrap(True); layout.addWidget(self.label)
        row = QHBoxLayout(); row.addStretch()
        self.mic = IconButton('mic', 'Включить микрофон', owner._toggle_microphone)
        self.stop = QPushButton('Стоп'); self.stop.clicked.connect(self.controller.cancel_response)
        self.pause = QPushButton('Пауза'); self.pause.clicked.connect(self.controller.toggle_pause)
        row.addWidget(self.mic); row.addWidget(self.stop); row.addWidget(self.pause); row.addStretch()
        layout.addLayout(row)
        self.return_button = QPushButton('Открыть главное окно')
        self.return_button.clicked.connect(owner._restore_main)
        layout.addWidget(self.return_button)
        close = QPushButton('Закрыть мини-окно'); close.clicked.connect(self.close)
        layout.addWidget(close)

    def _drag_press(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()
            event.accept()

    def _drag_move(self, event):
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def _drag_release(self, event):
        self._drag_offset = None
        event.accept()

    def sync(self, mode, label, level, phase):
        c = self.controller
        self.label.setText(label)
        self.orb.mode, self.orb.level, self.orb.phase = mode, level, phase
        self.orb.update()
        self.mic.active = c.capture is not None
        self.mic.setEnabled(not c.closing and not c.reconfiguring and c.mic_state != 'stopping')
        self.mic.setAccessibleName('Выключить микрофон' if self.mic.active else 'Включить микрофон')
        self.mic.setToolTip(self.mic.accessibleName()); self.mic.update()
        self.stop.setEnabled(c.guard.active is not None and not c.closing)
        self.pause.setEnabled(c.stage == 'playback' and not c.closing)
        self.pause.setText('Продолжить' if c.response_paused else 'Пауза')

    def closeEvent(self, event):
        # Closing this surface never shuts down the shared controller.
        self.hide()
        event.ignore()
