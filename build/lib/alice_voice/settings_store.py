"""Serialize atomic Settings writes off the GUI thread; coalesce fast edits."""
import logging
from pathlib import Path
from threading import Thread

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot

from .config import Settings

LOGGER = logging.getLogger(__name__)


class SaveSignals(QObject):
    saved = pyqtSignal(int, object)
    failed = pyqtSignal(int)


class SettingsWriter(Thread):
    def __init__(self, revision: int, settings: Settings, path: Path, signals: SaveSignals):
        super().__init__(name='alice-settings', daemon=False)
        self.revision, self.settings, self.path, self.signals = revision, settings, path, signals

    def run(self) -> None:
        try:
            self.settings.save(self.path)
        except Exception as error:
            LOGGER.error('stage=settings_save_failed error_type=%s', type(error).__name__)
            self.signals.failed.emit(self.revision)
        else:
            self.signals.saved.emit(self.revision, self.settings)


class SettingsStore(QObject):
    changed = pyqtSignal(object)
    status = pyqtSignal(str)
    idle = pyqtSignal()

    def __init__(self, controller, path: str | Path, parent=None):
        super().__init__(parent)
        self.controller, self.path = controller, Path(path)
        self.desired = Settings.from_dict(controller.settings.to_dict())
        self.persisted = self.desired
        self.revision = 0
        self.pending = None
        self.active = None
        self.worker = None
        self.closing = False
        self.signals = SaveSignals(self)
        self.signals.saved.connect(self._saved, Qt.ConnectionType.QueuedConnection)
        self.signals.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)

    @property
    def busy(self) -> bool:
        return self.active is not None or self.pending is not None

    def request(self, settings: Settings) -> None:
        if self.closing:
            return
        snapshot = Settings.from_dict(settings.to_dict())
        if snapshot == self.desired:
            return
        self.desired = snapshot
        self.revision += 1
        self.pending = (self.revision, snapshot)
        self.changed.emit(snapshot)
        self.status.emit('Сохранение настроек…')
        self._start_next()

    def _start_next(self) -> None:
        if self.active is not None or self.pending is None:
            return
        revision, settings = self.pending
        self.pending = None
        self.active = revision
        self.worker = SettingsWriter(revision, settings, self.path, self.signals)
        try:
            self.worker.start()
        except Exception:
            self._failed(revision)

    @pyqtSlot(int, object)
    def _saved(self, revision: int, settings: Settings) -> None:
        if revision != self.active:
            return
        self.active, self.worker = None, None
        self.persisted = settings
        if self.pending is None:
            if not self.controller.closing:
                self.controller.reconfigure(settings)
            self.status.emit('Настройки сохранены.' if not self.controller.reconfiguring
                             else 'Настройки сохранены. Перезапуск служб…')
        self._advance()

    @pyqtSlot(int)
    def _failed(self, revision: int) -> None:
        if revision != self.active:
            return
        self.active, self.worker = None, None
        if self.pending is None:
            self.desired = self.persisted
            # An earlier successful write may have been superseded before apply.
            if not self.controller.closing:
                self.controller.reconfigure(self.persisted)
            self.changed.emit(self.desired)
            self.status.emit('Не удалось сохранить настройки. Проверьте доступ к файлу.')
        self._advance()

    def _advance(self) -> None:
        if self.pending is not None:
            self._start_next()
        else:
            self.idle.emit()

    def shutdown(self) -> None:
        self.closing = True  # Authorized pending writes finish; new edits are ignored.
