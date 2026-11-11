"""Memory-only screen preview with asynchronous capture and explicit sending."""
from threading import Thread

from PyQt6.QtCore import QObject, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QRubberBand, QSizePolicy, QVBoxLayout,
)

from ..utils.vision import (
    active_window_region, capture_screen_jpeg, list_capture_monitors, map_preview_region,
)


class ScreenSignals(QObject):
    sources = pyqtSignal(object)
    captured = pyqtSignal(bytes, object)
    failed = pyqtSignal(str)


class ScreenWorker(Thread):
    def __init__(self, signals, *, discover=False, region=None, window=False):
        super().__init__(name='alice-screen-preview', daemon=True)
        self.signals, self.discover, self.region, self.window = signals, discover, region, window

    def run(self):
        try:
            if self.discover:
                self.signals.sources.emit(list_capture_monitors())
            else:
                region = active_window_region() if self.window else self.region
                image = capture_screen_jpeg(region=region)
                self.signals.captured.emit(image, region)
        except Exception:
            try:
                self.signals.failed.emit('Не удалось получить снимок или список экранов. Повторите захват.')
            except RuntimeError:
                pass  # Preview was destroyed while the native operation finished.


class ImagePreview(QLabel):
    region_selected = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__('Сделайте снимок, чтобы проверить изображение перед отправкой.', parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.setMinimumSize(240, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.original = QPixmap()
        self.native_region = None
        self.selectable = False
        self.origin = None
        self.band = QRubberBand(QRubberBand.Shape.Rectangle, self)

    def set_image(self, data, native_region):
        pixmap = QPixmap()
        if not pixmap.loadFromData(data, 'JPEG'):
            raise ValueError('Снимок не удалось открыть.')
        self.original, self.native_region = pixmap, dict(native_region)
        self._fit()

    def clear_image(self):
        self.original = QPixmap()
        self.native_region = None
        self.origin = None
        self.band.hide()
        self.setPixmap(QPixmap())

    def _fit(self):
        if not self.original.isNull():
            self.setPixmap(self.original.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def _image_rect(self):
        pixmap = self.pixmap()
        if pixmap is None or pixmap.isNull():
            return QRect()
        return QRect((self.width() - pixmap.width()) // 2, (self.height() - pixmap.height()) // 2,
                     pixmap.width(), pixmap.height())

    def mousePressEvent(self, event):
        position = event.position().toPoint()
        if self.selectable and event.button() == Qt.MouseButton.LeftButton and self._image_rect().contains(position):
            self.origin = position
            self.band.setGeometry(QRect(position, position))
            self.band.show()

    def mouseMoveEvent(self, event):
        if self.origin is not None:
            self.band.setGeometry(QRect(self.origin, event.position().toPoint()).normalized().intersected(self._image_rect()))

    def mouseReleaseEvent(self, event):
        if self.origin is None or event.button() != Qt.MouseButton.LeftButton:
            return
        selection = QRect(self.origin, event.position().toPoint()).normalized().intersected(self._image_rect())
        self.origin = None
        self.band.hide()
        if selection.width() < 8 or selection.height() < 8 or self.native_region is None:
            return
        display = self._image_rect()
        self.region_selected.emit(map_preview_region(
            (display.x(), display.y(), display.width(), display.height()),
            (selection.x(), selection.y(), selection.width(), selection.height()), self.native_region))


class ScreenVisionDialog(QDialog):
    def __init__(self, prompt, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Посмотреть экран — превью')
        self.resize(860, 640)
        self.image_jpeg = None
        self.busy = False
        self.closed_preview = False
        self.has_crop = False
        self.concealed = []
        self.worker = None
        self.capture_timer = QTimer(self)
        self.capture_timer.setSingleShot(True)
        self.capture_timer.timeout.connect(self._start_capture)
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.addItem('Монитор', 'monitor')
        self.mode_combo.addItem('Активное окно (через 3 с)', 'window')
        self.mode_combo.addItem('Область монитора', 'region')
        self.monitor_combo = QComboBox()
        self.capture_btn = QPushButton('Сделать снимок')
        row.addWidget(self.mode_combo)
        row.addWidget(self.monitor_combo, 1)
        row.addWidget(self.capture_btn)
        layout.addLayout(row)
        self.preview = ImagePreview()
        self.preview.setAccessibleName('Превью снимка; в режиме области выделите прямоугольник мышью')
        layout.addWidget(self.preview, 1)
        self.notice = QLabel('Получение мониторов…')
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.prompt = QPlainTextEdit(prompt)
        self.prompt.setAccessibleName('Вопрос к снимку')
        self.prompt.setMaximumHeight(85)
        layout.addWidget(self.prompt)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.send_btn = buttons.addButton('Отправить в Gemini', QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.signals = ScreenSignals(self)
        self.signals.sources.connect(self._sources_ready, Qt.ConnectionType.QueuedConnection)
        self.signals.captured.connect(self._captured, Qt.ConnectionType.QueuedConnection)
        self.signals.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        self.capture_btn.clicked.connect(self._capture)
        self.mode_combo.currentIndexChanged.connect(self._source_changed)
        self.monitor_combo.currentIndexChanged.connect(self._source_changed)
        self.preview.region_selected.connect(self._crop)
        self.prompt.textChanged.connect(self._update_controls)
        self._update_controls()
        self._launch_worker(ScreenWorker(self.signals, discover=True))

    def _launch_worker(self, worker):
        self.worker = worker
        try:
            worker.start()
        except Exception:
            self._failed('Не удалось запустить захват экрана. Повторите попытку.')

    def _sources_ready(self, monitors):
        if self.closed_preview:
            return
        self.monitor_combo.clear()
        for monitor in monitors:
            name = f"Монитор {monitor['index']} · {monitor['width']}×{monitor['height']}"
            if monitor['is_primary']:
                name += ' · основной'
            self.monitor_combo.addItem(name, monitor)
        primary = next((i for i, monitor in enumerate(monitors) if monitor['is_primary']), 0)
        self.monitor_combo.setCurrentIndex(primary)
        self._source_changed()

    def _source_changed(self):
        self.image_jpeg = None
        self.has_crop = False
        self.preview.clear_image()
        self.preview.selectable = self.mode_combo.currentData() == 'region'
        self.notice.setText('За 3 секунды после нажатия переключитесь на нужное окно.'
                            if self.mode_combo.currentData() == 'window' else
                            'Сделайте снимок. В режиме области выделите нужный фрагмент на превью.')
        self._update_controls()

    def _update_controls(self):
        available = self.mode_combo.currentData() == 'window' or self.monitor_combo.currentData() is not None
        self.capture_btn.setEnabled(available and not self.busy)
        self.mode_combo.setEnabled(not self.busy)
        self.monitor_combo.setEnabled(not self.busy and self.mode_combo.currentData() != 'window')
        self.preview.selectable = not self.busy and self.mode_combo.currentData() == 'region'
        cropped = self.mode_combo.currentData() != 'region' or self.has_crop
        self.send_btn.setEnabled(not self.busy and bool(self.image_jpeg) and cropped
                                 and bool(self.prompt.toPlainText().strip()))

    def _capture(self):
        if self.busy or self.closed_preview:
            return
        self.has_crop = False
        region = self.monitor_combo.currentData()
        self._queue_capture(region, self.mode_combo.currentData() == 'window')

    def _crop(self, region):
        if self.busy or self.closed_preview:
            return
        self.has_crop = True
        self._queue_capture(region, False)

    def _queue_capture(self, region, window):
        self.busy = True
        self.image_jpeg = None
        self.preview.clear_image()
        self.capture_region, self.capture_window = region, window
        self._update_controls()
        self.notice.setText('Захват через 3 секунды: переключитесь на нужное окно.' if window else 'Захват экрана…')
        # Keep the modal event loop alive while removing Alice from the image.
        self.concealed = []
        for widget in (self.parentWidget(), self):
            if widget is not None and widget.isVisible():
                self.concealed.append((widget, widget.windowOpacity(), widget.windowState()))
                widget.setWindowOpacity(0)
                if window:
                    widget.showMinimized()
        self.capture_timer.start(3000 if window else 150)

    def _start_capture(self):
        if self.closed_preview:
            return
        self._launch_worker(ScreenWorker(self.signals, region=self.capture_region, window=self.capture_window))

    def _restore_windows(self):
        for widget, opacity, state in self.concealed:
            if getattr(getattr(widget, 'controller', None), 'closing', False):
                continue
            widget.setWindowState(state)
            widget.setWindowOpacity(opacity)
        self.concealed.clear()
        if not self.closed_preview:
            self.raise_()
            self.activateWindow()

    def _captured(self, data, region):
        if self.closed_preview:
            return
        self.busy = False
        self._restore_windows()
        try:
            self.preview.set_image(data, region)
        except ValueError:
            self._failed('Не удалось открыть снимок. Повторите захват.')
            return
        self.image_jpeg = data
        self.notice.setText('Выделите прямоугольник на превью: выбранная область будет снята отдельно.'
                            if self.mode_combo.currentData() == 'region' and not self.has_crop else
                            f'Готово · {len(data) // 1024} КБ. Проверьте снимок и вопрос перед отправкой.')
        self._update_controls()

    def _failed(self, message):
        if self.closed_preview:
            return
        self.busy = False
        self._restore_windows()
        self.image_jpeg = None
        self.notice.setText(message)
        self._update_controls()

    def take_image(self):
        data, self.image_jpeg = self.image_jpeg, None
        self.preview.clear_image()
        return data

    def done(self, result):
        if result == QDialog.DialogCode.Accepted and not self.send_btn.isEnabled():
            return
        self.closed_preview = True
        self.capture_timer.stop()
        self._restore_windows()
        if result != QDialog.DialogCode.Accepted:
            self.take_image()
        super().done(result)
