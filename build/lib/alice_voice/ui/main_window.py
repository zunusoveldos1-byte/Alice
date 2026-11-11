"""WMC main window bound to the GUI-owned session controller."""
from dataclasses import replace
from pathlib import Path
from time import perf_counter

from PyQt6.QtCore import QEvent, QSignalBlocker, Qt, QTime, QTimer, pyqtSlot
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QDialog, QMainWindow, QPushButton, QSizeGrip, QSizePolicy,
)

from ..config import Settings
from ..settings_store import SettingsStore
from ..secrets import CredentialError
from .dialogs import MetricsDialog, SettingsDialog
from .cosmic_shell import CosmicShell
from .cosmic_widgets import CosmicBackground
from .cosmic_theme import FRAME_INTERVAL_MS
from .focus_style import install_focus_policy
from .vision_dialog import ScreenVisionDialog
from .widgets import EntranceFade


class MainWindow(CosmicShell, QMainWindow):
    def __init__(self, controller, settings_path: str | Path = 'settings.json'):
        install_focus_policy()
        super().__init__()
        self.controller, self.settings_path = controller, Path(settings_path)
        self.settings_store = SettingsStore(controller, self.settings_path, self)
        self.allow_close = self.backend_closed = False
        self.dirty = True
        self.last_render = 0.0
        self._last_frame = perf_counter()
        self.motion_enabled = True
        self.notice_text = ''
        self.drag_offset = None
        self.light = False
        self._shown_once = False
        self._dialog_fades: list[EntranceFade] = []
        self.settings_page = None
        self.setWindowTitle('Alice Voice')
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(1440, 900)
        self.setMinimumSize(480, 560)
        self.setFont(QFont('Segoe UI', 10))
        self.background = CosmicBackground(self)
        self.background.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setCentralWidget(self.background)
        self._build_shell()
        self.fullscreen_shortcut = QShortcut(QKeySequence('F11'), self)
        self.fullscreen_shortcut.activated.connect(self._toggle_fullscreen)
        self.exit_fullscreen_shortcut = QShortcut(QKeySequence('Esc'), self)
        self.exit_fullscreen_shortcut.activated.connect(self._exit_fullscreen)
        self.entrance = EntranceFade(self.content)
        self.size_grip = QSizeGrip(self.background)
        self.size_grip.setFixedSize(18, 18)
        self.size_grip.setToolTip('Изменить размер окна')
        self._apply_theme()
        self._sync_settings()
        self._adapt_layout()
        controller.changed.connect(self._request_render, Qt.ConnectionType.QueuedConnection)
        controller.notification.connect(self._notice, Qt.ConnectionType.QueuedConnection)
        controller.settings_applied.connect(self._settings_applied, Qt.ConnectionType.QueuedConnection)
        controller.closed.connect(self._backend_closed, Qt.ConnectionType.QueuedConnection)
        controller.screen_preview_requested.connect(self._show_screen_preview, Qt.ConnectionType.QueuedConnection)
        self.settings_store.changed.connect(self._sync_settings, Qt.ConnectionType.QueuedConnection)
        self.settings_store.status.connect(self._notice, Qt.ConnectionType.QueuedConnection)
        self.settings_store.status.connect(self._settings_page_status, Qt.ConnectionType.QueuedConnection)
        self.settings_store.idle.connect(self._try_finish_close, Qt.ConnectionType.QueuedConnection)
        self.render_timer = QTimer(self)
        self.render_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.render_timer.setInterval(FRAME_INTERVAL_MS)
        # Rearm after each frame: late events must not cause alternating 50/100ms frames.
        self.render_timer.setSingleShot(True)
        self.render_timer.timeout.connect(self._tick)
        self.render_timer.start()
        self._render()

    def _button(self, text: str, callback, parent=None) -> QPushButton:
        button = QPushButton(text, parent)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(callback)
        return button

    @pyqtSlot()
    def _request_render(self) -> None:
        self.dirty = True

    @pyqtSlot(str)
    def _notice(self, message: str) -> None:
        self.notice_text = message
        self.dirty = True

    def _mode(self) -> tuple[str, str]:
        c = self.controller
        if c.closing:
            return 'closing', 'Завершаю работу...'
        if c.reconfiguring:
            return 'loading', 'Применяю настройки...'
        if c.mic_state == 'error':
            return 'error', 'Ошибка микрофона'
        if c.last_error:
            return 'error', 'Не удалось получить ответ'
        if c.response_paused:
            return 'paused', 'Пауза'
        if c.stage == 'vision':
            return 'thinking', 'Смотрю на экран...'
        if c.stage in ('silero_download', 'silero_loading', 'silero_ready'):
            return 'loading', {'silero_download': 'Загружаю Silero из сети...',
                               'silero_loading': 'Загружаю Silero в память...',
                               'silero_ready': 'Silero готова'}[c.stage]
        if c.stage == 'playback' and c.current_chunk is not None and not c.playback.first_active:
            return 'speaking', 'Отвечаю'
        if c.stage in ('llm', 'tts', 'playback'):
            return 'thinking', 'Думаю'
        if c.mic_state == 'loading':
            return 'loading', 'Загружаю модели...'
        if c.mic_state == 'stopping':
            return 'loading', 'Выключаю микрофон...'
        if c.mic_state == 'listening':
            return 'listening', 'Я слушаю'
        return 'off', 'Микрофон выключен'

    def _tick(self) -> None:
        if self.allow_close:
            return
        self.render_timer.start()
        now = perf_counter()
        elapsed = now - self._last_frame
        main_visible = self.isVisible() and not self.isMinimized()
        mini_visible = self.mini is not None and self.mini.isVisible() and not self.mini.isMinimized()
        if not main_visible and not mini_visible:
            self._last_frame = now
            return
        self._last_frame = now
        if self.motion_enabled:
            if main_visible:
                self.background.advance(elapsed)
            self.state_label.advance(now)
            self.entrance.advance(now)
            for fade in self._dialog_fades:
                fade.advance(now)
        if self.clock.text() != QTime.currentTime().toString('HH:mm'):
            self.dirty = True
        if self.dirty or self.motion_enabled:
            self._render()

    def set_motion_enabled(self, enabled: bool) -> None:
        """Allow hosts to opt out of decorative motion and text transitions."""
        self.motion_enabled = enabled
        self._last_frame = perf_counter()
        if not enabled:
            self.state_label.finish_transition()
            self.entrance.finish()
            for fade in self._dialog_fades:
                fade.finish()
        self.dirty = True

    def _render(self) -> None:
        self.last_render = perf_counter()
        self.dirty = False
        c = self.controller
        mode, label = self._mode()
        self.state_label.set_status(label, self.last_render, animate=self.motion_enabled)
        self.mic_btn.mode = self.voice_stage.mode = mode
        self.mic_btn.phase = self.voice_stage.phase = self.last_render if self.motion_enabled else 0.0
        self.voice_stage.level = self._smoothed_level
        self.mic_btn.setEnabled(not c.closing and not c.reconfiguring and c.mic_state != 'stopping')
        action = 'Выключить микрофон' if c.capture is not None else 'Включить микрофон'
        self.mic_btn.setAccessibleName(action)
        self.mic_btn.setToolTip(action)
        self.mic_btn.update()
        self.voice_stage.update()
        if c.answer != self.answer_text.toPlainText():
            bar = self.answer_text.verticalScrollBar()
            at_bottom = bar.value() >= bar.maximum() - 2
            position = bar.value()
            self.answer_text.setPlainText(c.answer)
            bar.setValue(bar.maximum() if at_bottom else position)
        self.notice.setText(self.notice_text)
        self.stop_btn.setEnabled(c.guard.active is not None and not c.closing)
        self.screen_btn.setEnabled(not c.closing and not c.reconfiguring and c.guard.active is None
                                   and not c.screen_preview_active)
        self.pause_btn.setEnabled(c.stage == 'playback' and not c.closing)
        self.pause_btn.setText('Продолжить' if c.response_paused else 'Пауза')

        for widget in (self.model_combo, self.provider_combo, self.voice_combo, self.volume):
            widget.setEnabled(not c.closing)
        self.clock.setText(QTime.currentTime().toString('HH:mm'))
        if c.completed_turns:
            self.history_label.setText('\n\n'.join(f'Вы: {u}\nАлиса: {a}' for u,a in list(c.completed_turns)[-5:]))
        self._render_cosmic(mode)

    def _toggle_microphone(self) -> None:
        try:
            if self.controller.capture is None:
                self.controller.enable_microphone()
            else:
                self.controller.disable_microphone()
        except (ValueError, RuntimeError) as error:
            self._notice(str(error))
        self.dirty = True

    @pyqtSlot(object)
    def _sync_settings(self, settings=None) -> None:
        settings = self.settings_store.desired
        blockers = [QSignalBlocker(widget) for widget in
                    (self.provider_combo, self.model_combo, self.voice_combo, self.volume)]
        self.provider_combo.setCurrentIndex(self.provider_combo.findData(settings.provider))
        model_key = dict(ollama='ollama_model', gemini='gemini_model', lmstudio='lmstudio_model')[settings.provider]
        current = getattr(settings, model_key)
        defaults = Settings()
        default = getattr(defaults, model_key)
        self.model_combo.clear()
        self.model_combo.addItems(list(dict.fromkeys((current, default))))
        self.model_combo.setCurrentText(current)
        self.voice_combo.setCurrentIndex(self.voice_combo.findData(settings.synthesizer))
        self.volume.setValue(round(settings.volume * 100))
        self.volume_control.label.setText(f'{self.volume.value()}%')
        if self.motion_enabled == settings.reduced_motion:
            self.set_motion_enabled(not settings.reduced_motion)
        blocker = QSignalBlocker(self.reduced_motion)
        self.reduced_motion.setChecked(settings.reduced_motion)
        del blocker
        del blockers
        self.dirty = True

    @pyqtSlot(object)
    def _settings_applied(self, settings) -> None:
        self._notice('Настройки применены.')

    def _request_settings(self, settings: Settings) -> None:
        try:
            self.settings_store.request(settings)
        except ValueError as error:
            self._notice(str(error))
            self._sync_settings()

    def _provider_changed(self, index: int) -> None:
        self._request_settings(replace(self.settings_store.desired, provider=self.provider_combo.currentData()))

    def _model_changed(self, index=None) -> None:
        current = self.settings_store.desired
        key = dict(ollama='ollama_model', gemini='gemini_model', lmstudio='lmstudio_model')[current.provider]
        model = self.model_combo.currentText().strip()
        if not model:
            self._notice('Введите имя модели.')
            self._sync_settings()
            return
        self._request_settings(replace(current, **{key: model}))

    def _voice_changed(self, index: int) -> None:
        self._request_settings(replace(self.settings_store.desired, synthesizer=self.voice_combo.currentData()))

    def _volume_changed(self, value: int) -> None:
        volume = value / 100
        self.controller.set_volume(volume)
        self._request_settings(replace(self.settings_store.desired, volume=volume))

    def _show_settings(self) -> None:
        if self.settings_page is None:
            page = SettingsDialog(self.settings_store.desired, self, embedded=True)
            self.settings_page = page
            page.save_requested.connect(self._save_settings_page)
            page.cancel_requested.connect(self._discard_settings_page)
            self.pages.addWidget(page)
        self.select_page('settings')

    def _settings_page_status(self, text: str) -> None:
        if self.settings_page is not None:
            self.settings_page.error.setText(text)

    def _discard_settings_page(self) -> None:
        page, self.settings_page = self.settings_page, None
        self.select_page('voice')
        if page is not None:
            for field in (page.key, page.elevenlabs_key, page.lmstudio_token):
                field.clear()
            self.pages.removeWidget(page)
            page.deleteLater()

    def _save_settings_page(self) -> None:
        dialog = self.settings_page
        if dialog is None:
            return
        try:
            settings = dialog.values()
            dialog.save_credentials()
            self.settings_store.request(settings)
            dialog.original = settings
            dialog.error.setText('Сохранение…' if self.settings_store.busy else 'Настройки сохранены.')
        except (ValueError, CredentialError) as error:
            dialog.error.setText(str(error))
        finally:
            if dialog.credentials_changed:
                dialog.credentials_changed = False
                self.controller.reconfigure(
                    self.controller.pending_settings or self.controller.settings,
                    credentials_changed=True)

    def _show_screen_preview(self, prompt: str, utterance_id: str = '') -> None:
        c = self.controller
        if c.closing or c.reconfiguring or c.guard.active is not None:
            c.screen_preview_active = False
            return
        c.screen_preview_active = True
        dialog = ScreenVisionDialog(prompt, self)
        data = None
        question = ''
        def cancel_if_unavailable():
            if c.closing or c.reconfiguring or not c.screen_preview_active:
                dialog.reject()
        c.changed.connect(cancel_if_unavailable)
        try:
            if self._exec_dialog(dialog) == QDialog.DialogCode.Accepted:
                data = dialog.take_image()
                question = dialog.prompt.toPlainText().strip()
        finally:
            c.changed.disconnect(cancel_if_unavailable)
            c.screen_preview_active = False
            dialog.take_image()
            dialog.deleteLater()
            self.dirty = True
        if data is not None:
            c.request_screen(question, utterance_id=utterance_id or None, image_jpeg=data)

    def _show_metrics(self) -> None:
        self._exec_dialog(MetricsDialog(self.controller.metrics, self))

    def _exec_dialog(self, dialog: QDialog) -> int:
        fade = EntranceFade(dialog)
        self._dialog_fades.append(fade)
        fade.start(perf_counter(), enabled=self.motion_enabled)
        try:
            return dialog.exec()
        finally:
            fade.finish()
            self._dialog_fades.remove(fade)

    def _toggle_maximized(self) -> None:
        self.showNormal() if self.isMaximized() or self.isFullScreen() else self.showMaximized()

    def _toggle_fullscreen(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def _exit_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.Resize:
            self._adapt_layout()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._adapt_layout()
        if hasattr(self, 'size_grip'):
            self.size_grip.move(self.background.width() - 20, self.background.height() - 20)
            self.size_grip.raise_()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, 'maximize_btn'):
            expanded = self.isMaximized() or self.isFullScreen()
            self.maximize_btn.set_action('restore' if expanded else 'maximize')
            self.size_grip.setVisible(not expanded)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._shown_once:
            self._shown_once = True
            self.entrance.start(perf_counter(), enabled=self.motion_enabled)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.header.rect().translated(self.header.mapTo(self, self.header.rect().topLeft())).contains(event.position().toPoint()):
            handle = self.windowHandle()
            if handle is not None and handle.startSystemMove():
                return
            self.drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self.drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self.drag_offset = None
        super().mouseReleaseEvent(event)

    def closeEvent(self, event) -> None:
        self._stop_camera()
        if self.mini is not None:
            self.mini.close()
        if self.allow_close:
            self.render_timer.stop()
            event.accept()
            return
        event.ignore()
        self.settings_store.shutdown()
        self.controller.shutdown()
        self.dirty = True
        self._try_finish_close()

    @pyqtSlot()
    def _backend_closed(self) -> None:
        self.backend_closed = True
        self._try_finish_close()

    @pyqtSlot()
    def _try_finish_close(self) -> None:
        if self.controller.closing and self.backend_closed and not self.settings_store.busy:
            self.allow_close = True
            self.close()
