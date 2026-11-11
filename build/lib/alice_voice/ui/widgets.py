"""Paint-only WMC widgets: all state is supplied by the GUI timer."""
import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QPixmap, QRadialGradient
from PyQt6.QtWidgets import QFrame, QGraphicsOpacityEffect, QLabel, QPushButton, QWidget


class EntranceFade:
    """Short-lived opacity effect advanced by the existing GUI frame clock."""

    DURATION = .28

    def __init__(self, widget: QWidget):
        self.widget = widget
        self._effect: QGraphicsOpacityEffect | None = None
        self._started = 0.0

    def start(self, now: float, *, enabled: bool = True) -> None:
        self.finish()
        if not enabled:
            return
        self._started = now
        self._effect = QGraphicsOpacityEffect(self.widget)
        self._effect.setOpacity(0.0)
        self.widget.setGraphicsEffect(self._effect)

    def advance(self, now: float) -> None:
        if self._effect is None:
            return
        progress = min(1.0, max(0.0, (now - self._started) / self.DURATION))
        if progress >= 1:
            self.finish()
        else:
            self._effect.setOpacity(1 - (1 - progress) ** 3)

    def finish(self) -> None:
        if self._effect is not None:
            self._effect = None
            # Remove the effect and its offscreen surface after the entrance.
            self.widget.setGraphicsEffect(None)


class WindowControlButton(QPushButton):
    """DPI-independent caption glyphs with a generous keyboard/click target."""

    NAMES = {'minimize': 'Свернуть', 'maximize': 'Развернуть',
             'restore': 'Восстановить размер', 'close': 'Закрыть'}

    def __init__(self, action: str, parent=None):
        super().__init__(parent)
        self.setFixedSize(40, 40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.light = False
        self.set_action(action)

    def set_action(self, action: str) -> None:
        self.action = action
        self.setToolTip(self.NAMES[action])
        self.setAccessibleName(self.NAMES[action])
        self.update()

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(self.width() / 40, self.height() / 40)
        painter.setPen(Qt.PenStyle.NoPen)
        if self.underMouse() or (self.hasFocus() and self.property('keyboardFocus')) or self.isDown():
            accent = QColor('#ff5579' if self.action == 'close' else '#00c9ff')
            glow = QRadialGradient(QPointF(20, 20), 25)
            glow.setColorAt(0, QColor(accent.red(), accent.green(), accent.blue(), 115 if self.isDown() else 70))
            glow.setColorAt(1, QColor(accent.red(), accent.green(), accent.blue(), 15))
            painter.setBrush(glow)
            painter.drawRoundedRect(QRectF(1, 1, 38, 38), 9, 9)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        color = QColor('#062660' if self.light else '#e1f5ff')
        painter.setPen(QPen(color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if self.action == 'minimize':
            painter.drawLine(QPointF(14, 21), QPointF(26, 21))
        elif self.action == 'maximize':
            painter.drawRoundedRect(QRectF(14, 14, 12, 12), 1, 1)
        elif self.action == 'restore':
            path = QPainterPath(QPointF(16, 13))
            path.lineTo(27, 13)
            path.lineTo(27, 24)
            painter.drawPath(path)
            painter.drawRoundedRect(QRectF(12, 17, 11, 11), 1, 1)
        else:
            painter.drawLine(QPointF(14, 14), QPointF(26, 26))
            painter.drawLine(QPointF(26, 14), QPointF(14, 26))
        if self.hasFocus() and self.property('keyboardFocus'):
            painter.setPen(QPen(color, 1, Qt.PenStyle.SolidLine))
            painter.drawRoundedRect(QRectF(3, 3, 34, 34), 7, 7)


class StatusLabel(QLabel):
    """Crossfade driven by the window's frame clock, with no queued animations."""

    DURATION = .20

    def __init__(self, parent=None):
        super().__init__(parent)
        self.progress = 1.0
        self._previous_frame: QPixmap | None = None
        self._started = 0.0
        self.setTextFormat(Qt.TextFormat.PlainText)

    def set_status(self, text: str, now: float, *, animate: bool = True) -> None:
        if text == self.text():
            return
        has_previous = bool(self.text())
        # Keep the visible blend when another state arrives during a transition.
        # Only one small, transparent label-sized pixmap is retained.
        self._previous_frame = self._snapshot() if animate and has_previous else None
        self._started = now
        self.progress = 0.0 if animate and has_previous else 1.0
        # Expose the latest state immediately to accessibility clients and tests.
        self.setText(text)
        self.update()

    def advance(self, now: float) -> None:
        if self.progress < 1.0:
            self.progress = min(1.0, max(0.0, (now - self._started) / self.DURATION))
            if self.progress >= 1.0:
                self._previous_frame = None
            self.update()

    def finish_transition(self) -> None:
        self.progress = 1.0
        self._previous_frame = None
        self.update()

    def _snapshot(self) -> QPixmap:
        ratio = self.devicePixelRatioF()
        frame = QPixmap(math.ceil(self.width() * ratio), math.ceil(self.height() * ratio))
        frame.setDevicePixelRatio(ratio)
        frame.fill(Qt.GlobalColor.transparent)
        painter = QPainter(frame)
        self._draw_transition(painter)
        painter.end()
        return frame

    def paintEvent(self, event) -> None:
        if self.progress >= 1.0:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        self._draw_transition(painter)

    def _draw_transition(self, painter: QPainter) -> None:
        painter.setFont(self.font())
        painter.setPen(self.palette().color(QPalette.ColorRole.WindowText))
        opacity = 1 - (1 - self.progress) ** 3
        if self._previous_frame is not None:
            painter.setOpacity(1 - opacity)
            painter.drawPixmap(0, 0, self._previous_frame)
        painter.setOpacity(opacity)
        flags = self.alignment().value
        if self.wordWrap():
            flags |= Qt.TextFlag.TextWordWrap.value
        painter.drawText(self.contentsRect(), flags, self.text())


class GlassCard(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('glassCard')


class VoiceLogo(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(38, 46)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#00c9ff'), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for x, height in zip((5, 12, 19, 26, 33), (12, 25, 38, 27, 14)):
            painter.drawLine(QPointF(x, 23 - height / 2), QPointF(x, 23 + height / 2))


class MicrophoneButton(QPushButton):
    COLORS = {'off': '#46779f', 'listening': '#00d6ec', 'thinking': '#a18cff',
              'speaking': '#00d9ff', 'paused': '#d6bb75', 'loading': '#8eb5ff',
              'error': '#ff708b', 'closing': '#46779f'}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(204, 204)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName('Включить микрофон')
        self.mode, self.phase = 'off', 0.0

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Keep the icon, focus ring and glow proportional at every DPI scale.
        painter.scale(self.width() / 164, self.height() / 164)
        center = QPointF(82, 82)
        accent = QColor(self.COLORS[self.mode])
        active = self.mode not in ('off', 'error', 'closing')
        pulse = .5 + .5 * math.sin(self.phase * 2.5) if active else .2
        glow = QRadialGradient(center, 82)
        glow.setColorAt(0, QColor(accent.red(), accent.green(), accent.blue(), 145))
        glow.setColorAt(.72, QColor(accent.red(), accent.green(), accent.blue(), 125 if active else 65))
        glow.setColorAt(.88, QColor(accent.red(), accent.green(), accent.blue(), 55 if active else 25))
        glow.setColorAt(1, QColor(accent.red(), accent.green(), accent.blue(), 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(QRectF(0, 0, 164, 164))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for radius in (72 + pulse * 4, 79):
            color = QColor(accent)
            color.setAlpha(90 if active else 35)
            painter.setPen(QPen(color, 1))
            painter.drawEllipse(center, radius, radius)
        gradient = QLinearGradient(35, 20, 130, 150)
        gradient.setColorAt(0, QColor('#04cdf5' if active else '#21618f'))
        gradient.setColorAt(.55, QColor('#0066de' if active else '#163e73'))
        gradient.setColorAt(1, QColor('#092d99' if active else '#122b4c'))
        painter.setBrush(gradient)
        painter.setPen(QPen(accent.lighter(150), 2 if self.isEnabled() else 1))
        radius = 60 if self.isDown() else 63
        painter.drawEllipse(center, radius, radius)
        painter.setBrush(QColor('#e1f5ff'))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(QRectF(72, 47, 20, 42), 10, 10)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        pen = QPen(QColor('#ddf3ff'), 4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        arc = QPainterPath(QPointF(62, 79))
        arc.cubicTo(62, 111, 102, 111, 102, 79)
        painter.drawPath(arc)
        painter.drawLine(QPointF(82, 103), QPointF(82, 117))
        painter.drawLine(QPointF(71, 117), QPointF(93, 117))
        if self.hasFocus() and self.property('keyboardFocus'):
            painter.setPen(QPen(QColor('#ffffff'), 1, Qt.PenStyle.SolidLine))
            painter.drawEllipse(center, 68, 68)


class VoiceStage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(216)
        self.mode, self.level, self.phase = 'off', 0.0, 0.0

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(MicrophoneButton.COLORS[self.mode])
        painter.setPen(QPen(color, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        width, middle = self.width(), self.height() / 2
        active = self.mode in ('listening', 'thinking', 'speaking', 'loading')
        # RMS is the live mic level; thinking/speaking are explicitly status animation.
        amplitude = min(1.0, self.level * 14) if self.mode == 'listening' else .25
        if not active:
            amplitude = 0
        for x in range(12, width - 12, 9):
            if abs(x - width / 2) < 114:
                continue
            envelope = math.sin(math.pi * x / max(1, width)) ** 2
            movement = abs(math.sin(x * .035 + self.phase * 2.5))
            height = 1.2 + 38 * amplitude * envelope * movement
            painter.drawLine(QPointF(x, middle - height), QPointF(x, middle + height))


class GlassBackground(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.light = False
        self.phase = 0.0
        self._scene: QPixmap | None = None
        self._scene_key = None
        self._waves: tuple[QPixmap, ...] = ()
        self._wave_height = 0.0

    def advance(self, seconds: float) -> None:
        # A full cycle takes about a minute; bounded phase avoids precision loss.
        self.phase = (self.phase + min(.1, max(0.0, seconds)) * .10) % math.tau
        self.update()

    def paintEvent(self, event) -> None:
        key = (self.width(), self.height(), self.devicePixelRatioF(), self.light)
        if key != self._scene_key:
            self._scene = self._build_scene()
            self._waves = self._build_wave_layers()
            self._scene_key = key
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()), 22, 22)
        painter.setClipPath(clip)
        # Move a cached scene rather than rasterizing large translucent gradients
        # every frame. Overscan keeps the rounded edges filled throughout motion.
        dx, dy = 8 * math.sin(self.phase), 12 * math.sin(self.phase * 2)
        painter.drawPixmap(QRectF(dx - 32, dy - 32, self.width() + 64, self.height() + 64),
                           self._scene, QRectF(self._scene.rect()))
        # Two independently drifting sine ribbons cross along the bottom edge.
        for index, wave in enumerate(self._waves):
            dx = 28 * math.sin(self.phase + index * 2.1)
            dy = 9 * math.sin(self.phase * 2 + index * 1.3)
            target = QRectF(dx - 64, self.height() - self._wave_height - 32 + dy,
                            self.width() + 128, self._wave_height + 64)
            painter.drawPixmap(target, wave, QRectF(wave.rect()))

    def _build_scene(self) -> QPixmap:
        width, height, ratio = self.width(), self.height(), self.devicePixelRatioF()
        scene = QPixmap(math.ceil((width + 64) * ratio), math.ceil((height + 64) * ratio))
        scene.setDevicePixelRatio(ratio)
        painter = QPainter(scene)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(32, 32)
        gradient = QLinearGradient(0, 0, width, height)
        gradient.setColorAt(0, QColor('#c6edff' if self.light else '#020d1d'))
        gradient.setColorAt(.55, QColor('#8dcfff' if self.light else '#061d42'))
        gradient.setColorAt(1, QColor('#2a99f1' if self.light else '#034bad'))
        painter.fillRect(QRectF(-32, -32, width + 64, height + 64), gradient)
        for center, radius in ((QPointF(0, height * .88), width * .5),
                               (QPointF(width, height * .2), width * .32)):
            halo = QRadialGradient(center, max(1.0, radius))
            halo.setColorAt(0, QColor(0, 210, 255, 60 if self.light else 42))
            halo.setColorAt(1, QColor(0, 140, 255, 0))
            painter.fillRect(QRectF(-32, -32, width + 64, height + 64), halo)
        for offset, opacity in ((0, 170), (52, 100), (-40, 70)):
            ribbon = QPainterPath(QPointF(-64, height * .55 + offset))
            ribbon.cubicTo(width * .3, height * .18 + offset,
                           width * .45, height * 1.24 + offset, width + 64, height * .65 + offset)
            ribbon.lineTo(width + 64, height * .84 + offset)
            ribbon.cubicTo(width * .42, height * 1.1 + offset,
                           width * .25, height * .45 + offset, -64, height * .8 + offset)
            ribbon.closeSubpath()
            fill = QLinearGradient(0, height * .55, width, height)
            fill.setColorAt(0, QColor(0, 176, 255, opacity))
            fill.setColorAt(.5, QColor(2, 73, 234, opacity))
            fill.setColorAt(1, QColor(0, 224, 255, opacity))
            painter.fillPath(ribbon, fill)
        painter.end()
        return scene

    def _build_wave_layers(self) -> tuple[QPixmap, ...]:
        """Rasterize only on resize/theme/DPI changes, never on animation frames."""
        width, ratio = self.width(), self.devicePixelRatioF()
        band = self._wave_height = max(90.0, min(200.0, self.height() * .24))
        layers = []
        for index in range(2):
            layer = QPixmap(math.ceil((width + 128) * ratio), math.ceil((band + 64) * ratio))
            layer.setDevicePixelRatio(ratio)
            layer.fill(Qt.GlobalColor.transparent)
            painter = QPainter(layer)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.translate(64, 32)
            amplitude = band * (.18 + index * .04)
            frequency = math.tau / max(1, width)
            offset = index * 1.1

            def point(x: float) -> tuple[float, float]:
                angle = x * frequency + offset
                return band * .3 + amplitude * math.sin(angle), amplitude * frequency * math.cos(angle)

            x = -64.0
            y, slope = point(x)
            path = QPainterPath(QPointF(x, y))
            step = (width + 128) / 16
            for _ in range(16):
                next_x = x + step
                next_y, next_slope = point(next_x)
                path.cubicTo(x + step / 3, y + slope * step / 3,
                             next_x - step / 3, next_y - next_slope * step / 3, next_x, next_y)
                x, y, slope = next_x, next_y, next_slope
            path.lineTo(width + 64, band + 32)
            path.lineTo(-64, band + 32)
            path.closeSubpath()
            fill = QLinearGradient(0, 0, 0, band + 32)
            fill.setColorAt(0, QColor(0, 220 if index else 110, 255, 105 if index else 75))
            fill.setColorAt(1, QColor(0, 65, 235, 12))
            painter.fillPath(path, fill)
            painter.end()
            layers.append(layer)
        return tuple(layers)
