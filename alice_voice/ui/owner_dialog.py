"""User-started, cancellable speaker enrollment; recordings live only in RAM."""
import asyncio
from pathlib import Path
from threading import Event, Thread
from time import monotonic

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton

from ..owner_voice import OwnerProfile, SpeakerEngine


class EnrollmentSignals(QObject):
    status = pyqtSignal(str)
    result = pyqtSignal(bool, str)


class EnrollmentWorker(Thread):
    def __init__(self, settings, signals, cancel):
        super().__init__(name='alice-enrollment', daemon=False)
        self.settings, self.signals, self.cancel = settings, signals, cancel

    def run(self):
        try:
            import sounddevice as sd
            from ..vad import SileroVAD
            self.signals.status.emit('Подготовка модели… Микрофон откроется только для трёх образцов.')
            engine = SpeakerEngine(self.settings.speaker_model)
            vad = SileroVAD(self.settings.vad_model)
            vectors = []
            prompts = ('Меня зовут Элдос. Алиса, открой браузер и найди полезную информацию.',
                       'Алиса, запомни мои предпочтения. Сегодня я проверяю своего помощника.',
                       'Я говорю своим обычным голосом. Алиса, расскажи, какие у меня планы.')
            for index, prompt in enumerate(prompts):
                if self.cancel.is_set():
                    raise asyncio.CancelledError
                self.signals.status.emit(f'Образец {index + 1} из 3. Читайте вслух 7 секунд:\n\n{prompt}')
                chunks = []
                with sd.RawInputStream(device=self.settings.input_device, samplerate=16000,
                                       channels=1, dtype='int16', blocksize=512) as stream:
                    deadline = monotonic() + 7
                    while monotonic() < deadline:
                        if self.cancel.is_set():
                            raise asyncio.CancelledError
                        data, overflow = stream.read(512)
                        if overflow:
                            raise RuntimeError('Потеря аудио при записи. Закройте лишние программы и повторите.')
                        chunks.append(bytes(data))
                vad.reset()
                speech = [i for i, data in enumerate(chunks) if vad.predict(data) >= .5]
                if len(speech) < 40:
                    raise RuntimeError('В образце слишком мало речи. Говорите обычным голосом ближе к микрофону.')
                first, last = max(0, speech[0] - 4), min(len(chunks), speech[-1] + 5)
                self.signals.status.emit(f'Проверяю образец {index + 1}…')
                vectors.append(engine.embedding(b''.join(chunks[first:last])))
                chunks.clear()
            if self.cancel.is_set():
                raise asyncio.CancelledError
            OwnerProfile(Path(self.settings.data_directory) / 'owner.json').enroll(vectors, engine.fingerprint, self.cancel)
            self.signals.result.emit(True, 'Голос зарегистрирован. Проверка владельца не включается автоматически; при необходимости включите её отдельно в настройках.')
        except asyncio.CancelledError:
            self.signals.result.emit(False, 'Регистрация отменена. Профиль не изменён.')
        except (ValueError, RuntimeError) as error:
            self.signals.result.emit(False, str(error))
        except Exception:
            self.signals.result.emit(False, 'Не удалось записать голос. Проверьте микрофон, модель и права записи.')


class OwnerEnrollmentDialog(QDialog):
    enrolled = pyqtSignal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.cancel = Event()
        self.worker = None
        self.cancel_pending = False
        self.setWindowTitle('Только мой голос — регистрация')
        self.resize(580, 350)
        layout = QVBoxLayout(self)
        self.status = QLabel('Запишите три образца по 7 секунд в тихой комнате.\n'
            'Нажмите «Начать», затем читайте появляющиеся фразы.\n\n'
            'Аудио не сохраняется: на диске останется только вектор голоса.\n'
            'Проверка говорящего может ошибаться и не защищает от записи вашего голоса.\n'
            'Проверка включается отдельно. На короткой речи личность может не определиться; удержание кнопки её не требует.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.start = QPushButton('Начать регистрацию')
        self.start.clicked.connect(self._start)
        layout.addWidget(self.start)
        self.close_button = QPushButton('Закрыть')
        self.close_button.clicked.connect(self.reject)
        layout.addWidget(self.close_button)
        self.signals = EnrollmentSignals(self)
        self.signals.status.connect(self.status.setText, Qt.ConnectionType.QueuedConnection)
        self.signals.result.connect(self._result, Qt.ConnectionType.QueuedConnection)

    def _start(self):
        self.cancel.clear()
        self.cancel_pending = False
        self.start.setEnabled(False)
        self.close_button.setText('Отменить')
        self.worker = EnrollmentWorker(self.settings, self.signals, self.cancel)
        try:
            self.worker.start()
        except Exception:
            self._result(False, 'Не удалось запустить регистрацию.')

    def _result(self, ok, message):
        self.status.setText(message)
        self.start.setEnabled(True)
        self.close_button.setEnabled(True)
        self.close_button.setText('Закрыть')
        self.worker = None
        if ok:
            self.enrolled.emit()
        if self.cancel_pending:
            super().reject()

    def reject(self):
        if self.worker is not None:
            self.cancel_pending = True
            self.cancel.set()
            self.close_button.setEnabled(False)
        else:
            super().reject()

    def closeEvent(self, event):
        if self.worker is not None:
            event.ignore()
            self.reject()
        else:
            event.accept()
