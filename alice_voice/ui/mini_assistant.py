"""Independent floating surface; the main window owns the only voice session."""
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QPainter, QPainterPath, QPen, QColor, QRegion
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
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(STYLE)
        self.fair_active=False
        self.resize(288, 370)
        self._drag_offset = None
        layout = QVBoxLayout(self)
        self.drag_handle = QLabel('Alice Voice · перетащите окно')
        self.drag_handle.setFixedHeight(32)
        self.drag_handle.setCursor(Qt.CursorShape.SizeAllCursor)
        self.drag_handle.mousePressEvent = self._drag_press
        self.drag_handle.mouseMoveEvent = self._drag_move
        self.drag_handle.mouseReleaseEvent = self._drag_release
        layout.addWidget(self.drag_handle)
        self.fair_badge=QLabel('ЯРМАРКА')
        self.fair_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.fair_badge.setStyleSheet('color: #FFD3A1; background: #5A2B0D; border: 1px solid #FF9B38; border-radius: 10px; padding: 6px; font-weight: 600;')
        self.fair_badge.hide()
        layout.addWidget(self.fair_badge)
        self.orb = CosmicOrb(); self.orb.setFixedSize(190, 190)
        layout.addWidget(self.orb, alignment=Qt.AlignmentFlag.AlignCenter)
        self.label = QLabel(); self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setWordWrap(True); layout.addWidget(self.label)
        row = QHBoxLayout(); row.addStretch()
        self.mic = IconButton('mic', 'Включить микрофон', owner._toggle_microphone)
        self.stop = QPushButton('Стоп'); self.stop.clicked.connect(getattr(self.controller, 'stop_actions', self.controller.cancel_response))
        self.pause = QPushButton('Пауза'); self.pause.clicked.connect(self.controller.toggle_pause)
        row.addWidget(self.mic); row.addWidget(self.stop); row.addWidget(self.pause); row.addStretch()
        layout.addLayout(row)
        self.return_button = QPushButton('Открыть главное окно')
        self.return_button.clicked.connect(owner._restore_main)
        layout.addWidget(self.return_button)
        self.ptt=QPushButton('Удерживать, чтобы говорить')
        self.ptt.pressed.connect(self.controller.begin_ptt)
        self.ptt.released.connect(self.controller.end_ptt)
        layout.addWidget(self.ptt)
        close = QPushButton('Закрыть мини-окно'); close.clicked.connect(self.close)
        layout.addWidget(close)

    def corner_radius(self):
        return min(self.width(),self.height())*.20

    def resizeEvent(self,event):
        super().resizeEvent(event)
        radius=self.corner_radius()
        path=QPainterPath();path.addRoundedRect(QRectF(self.rect()),radius,radius)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))
        margin=max(24,round(radius*.45))
        if self.layout():self.layout().setContentsMargins(margin,margin,margin,margin)

    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#FF9B38' if self.fair_active else '#315477'),2 if self.fair_active else 1))
        painter.setBrush(QColor('#25190F' if self.fair_active else '#08172A'))
        radius=self.corner_radius()
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1,1,-1,-1),radius,radius)

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
        from ..fair_mode import enabled
        self.fair_active=enabled(c.settings)
        self.fair_badge.setVisible(self.fair_active)
        self.orb.fair_active=self.fair_active
        self.update()
        self.ptt.setEnabled(enabled(c.settings) and not c.closing and not c.reconfiguring)
        self.label.setText(label)
        self.orb.mode, self.orb.level, self.orb.phase = mode, level, phase
        self.orb.update()
        self.mic.active = c.capture is not None
        self.mic.setEnabled(not c.closing and not c.reconfiguring and c.mic_state != 'stopping')
        self.mic.setAccessibleName('Выключить микрофон' if self.mic.active else 'Включить микрофон')
        self.mic.setToolTip(self.mic.accessibleName()); self.mic.update()
        self.stop.setEnabled((c.guard.active is not None or getattr(c, 'automation_active', False) or getattr(c, 'power_pending', False)) and not c.closing)
        self.pause.setEnabled(c.stage == 'playback' and not c.closing)
        self.pause.setText('Продолжить' if c.response_paused else 'Пауза')

    def closeEvent(self, event):
        self.controller.end_ptt()
        # Closing this surface never shuts down the shared controller.
        self.hide()
        event.ignore()
