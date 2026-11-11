from dataclasses import replace
from PyQt6.QtCore import Qt, QTimer, QObject, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QPlainTextEdit, QDialog, QFormLayout, QLineEdit,
    QComboBox, QSpinBox, QDoubleSpinBox, QCheckBox, QDialogButtonBox, QTabWidget,
    QListWidget, QStackedWidget, QScrollArea, QFrame,
)
from ..workers import DeviceDiscoverySignals, MicrophoneDiscovery
from .. import secrets

METRIC_LABELS = {
    'audio_queue': 'Очередь кадров', 'vad': 'VAD / кадр', 'endpoint_wait': 'Ожидание тишины',
    'stt_finalize': 'Финализация STT', 'speech_end_to_text': 'Конец речи → текст',
    'llm_first': 'LLM: первый текст', 'llm_full': 'LLM: полный ответ', 'tts': 'Подготовка TTS',
    'llm_first_sentence': 'LLM: первый фрагмент', 'llm_to_first_pcm': 'LLM → первый PCM',
    'tts_first_pcm': 'TTS: первый PCM', 'provider_warmup': 'Прогрев провайдера',
    'pcm_to_playback': 'PCM → запуск выхода', 'speech_end_to_voice': 'Конец речи → запуск выхода',
    'cold_vad': 'Холодная загрузка VAD', 'cold_stt': 'Холодная загрузка Vosk',
}


class ConnectionSignals(QObject):
    result = pyqtSignal(int, object, str)


class SettingsDialog(QDialog):
    save_requested = pyqtSignal()
    cancel_requested = pyqtSignal()

    def __init__(self, settings, parent=None, *, embedded=False):
        super().__init__(parent)
        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
        self.setWindowTitle('Настройки Alice Voice')
        self.resize(600, 470)
        self.original = settings
        self.credentials_changed = False
        self.inputs = {}
        layout = QVBoxLayout(self)
        tabs = QTabWidget() if not embedded else None
        if embedded:
            split = QHBoxLayout()
            self.categories = QListWidget(); self.categories.setMaximumWidth(195)
            self.categories.setMinimumWidth(115)
            self.sections = QStackedWidget()
            self.categories.currentRowChanged.connect(self.sections.setCurrentIndex)
            split.addWidget(self.categories); split.addWidget(self.sections, 1)
            layout.addLayout(split, 1)
        else:
            layout.addWidget(tabs)
        def tab(name):
            widget = QWidget()
            widget.setObjectName('settingsSection')
            section_layout = QVBoxLayout(widget)
            form = QFormLayout()
            section_layout.addLayout(form)
            section_layout.addStretch(1)
            form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
            form.setVerticalSpacing(14)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            scroll = QScrollArea(); scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame); scroll.setWidget(widget)
            if embedded:
                self.categories.addItem(name); self.sections.addWidget(scroll)
            else:
                tabs.addTab(scroll, name)
            return form
        def line(form, label, key):
            entry = QPlainTextEdit(str(getattr(settings, key))) if key == 'system_prompt' else QLineEdit(str(getattr(settings, key)))
            if key == 'system_prompt':
                entry.setMinimumHeight(120)
                entry.setMaximumHeight(180)
                entry.setAccessibleName(label)
            self.inputs[key] = entry
            form.addRow(label, entry)
        def combo(form, label, key, choices):
            entry = QComboBox()
            entry.addItems(choices)
            entry.setCurrentText(getattr(settings, key))
            self.inputs[key] = entry
            form.addRow(label, entry)
        def number(form, label, key, minimum, maximum, floating=False):
            entry = QDoubleSpinBox() if floating else QSpinBox()
            entry.setRange(minimum, maximum)
            entry.setValue(getattr(settings, key))
            self.inputs[key] = entry
            form.addRow(label, entry)
        general = tab('Общие') if embedded else None
        llm = tab('Модели и подключения') if embedded else None
        voice = tab('Голос и озвучка') if embedded else None
        audio = tab('Устройства и STT' if embedded else 'Аудио / STT')
        device = QComboBox()
        device.addItem('Системный микрофон по умолчанию', None)
        if settings.input_device is not None:
            device.addItem(f'Микрофон №{settings.input_device} — загрузка названия…', settings.input_device)
            device.setCurrentIndex(1)
        self.inputs['input_device'] = device
        device_row = QHBoxLayout()
        device_row.addWidget(device, 1)
        self.refresh_devices = QPushButton('Обновить')
        self.refresh_devices.clicked.connect(self._refresh_devices)
        device_row.addWidget(self.refresh_devices)
        audio.addRow('Микрофон', device_row)
        self.device_notice = QLabel('Получение устройств…')
        self.device_notice.setWordWrap(True)
        audio.addRow(self.device_notice)
        self.device_signals = DeviceDiscoverySignals(self)
        self.device_signals.devices.connect(self._devices_ready, Qt.ConnectionType.QueuedConnection)
        self.device_signals.failed.connect(self._devices_failed, Qt.ConnectionType.QueuedConnection)
        self.device_worker = None
        self.available_microphones = None
        line(audio, 'Устройство выхода (пусто = стандартное)', 'output_device')
        line(audio, 'Silero ONNX', 'vad_model')
        line(audio, 'Каталог русской модели Vosk', 'vosk_model')
        number(audio, 'Порог начала речи', 'threshold', .01, .99, True)
        self.inputs['threshold'].setToolTip('Вероятность речи Silero, не громкость. Выше — строже, но тихую речь легче пропустить.')
        number(audio, 'Минимум речи, мс', 'min_speech_ms', 0, 1000)
        self.inputs['min_speech_ms'].setToolTip('По умолчанию 320 мс кадров речи без тишины и преролла. 0 отключает фильтр.')
        number(audio, 'Тишина, мс', 'silence_ms', 32, 3000)
        number(audio, 'Преролл, мс', 'preroll_ms', 0, 1000)
        number(audio, 'Максимальная фраза, с', 'max_phrase_s', 1, 120, True)
        if llm is None: llm = tab('Модель')
        combo(llm, 'Провайдер', 'provider', ['ollama', 'gemini'])
        self.inputs['provider'].addItem('LM Studio · локально', 'lmstudio')
        if settings.provider == 'lmstudio':
            self.inputs['provider'].setCurrentIndex(self.inputs['provider'].findData('lmstudio'))
        line(llm, 'URL Ollama', 'ollama_url')
        line(llm, 'Модель Ollama', 'ollama_model')
        line(llm, 'Модель Gemini', 'gemini_model')
        line(llm, 'URL LM Studio', 'lmstudio_url')
        lm_model = QComboBox()
        lm_model.setEditable(True)
        lm_model.addItem(settings.lmstudio_model)
        self.inputs['lmstudio_model'] = lm_model
        llm.addRow('Модель LM Studio (ID)', lm_model)
        self.lmstudio_token = QLineEdit()
        self.lmstudio_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.lmstudio_token.setPlaceholderText('Необязательный токен; пусто = не менять')
        self.delete_lmstudio = QCheckBox('Удалить токен')
        lm_token_row = QHBoxLayout()
        lm_token_row.addWidget(self.lmstudio_token, 1)
        lm_token_row.addWidget(self.delete_lmstudio)
        llm.addRow('Токен LM Studio', lm_token_row)
        self.check_lmstudio = QPushButton('Проверить подключение и модель')
        self.check_lmstudio.clicked.connect(self._check_lmstudio)
        llm.addRow(self.check_lmstudio)
        self.refresh_models = QPushButton('Получить список моделей с сервера')
        self.refresh_models.clicked.connect(lambda: self._check_lmstudio(list_only=True))
        llm.addRow(self.refresh_models)
        self.lmstudio_notice = QLabel('Запустите Local Server. Выберите фактический ID из списка моделей.\n'
                                        'Thinking отключайте в LM Studio, если модель поддерживает этот режим.')
        self.lmstudio_notice.setWordWrap(True)
        llm.addRow(self.lmstudio_notice)
        self.connection_signals = ConnectionSignals(self)
        self.connection_signals.result.connect(self._lmstudio_result, Qt.ConnectionType.QueuedConnection)
        self._lm_check_serial = 0
        self._lm_check_fingerprint = None
        number(llm, 'Таймаут, с', 'timeout_s', 1, 180, True)
        common = general if general is not None else llm
        number(common, 'Реплик в истории', 'history_turns', 0, 20)
        number(common, 'Лимит токенов', 'max_tokens', 16, 2048)
        number(common, 'Лимит символов ответа', 'max_response_chars', 32, 5000)
        line(common, 'Системный промпт', 'system_prompt')
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText('Новый ключ; сохранённый ключ не показывается')
        self.delete_gemini = QCheckBox('Удалить сохранённый ключ')
        gemini_row = QHBoxLayout()
        gemini_row.addWidget(self.key, 1)
        gemini_row.addWidget(self.delete_gemini)
        llm.addRow('Ключ Gemini', gemini_row)
        if voice is None: voice = tab('Голос')
        combo(voice, 'Синтез', 'synthesizer', ['sapi', 'edge', 'elevenlabs'])
        self.inputs['synthesizer'].addItem('Silero — локально', 'silero')
        if settings.synthesizer == 'silero':
            self.inputs['synthesizer'].setCurrentText('Silero — локально')
        combo(voice, 'Модель Silero (русский)', 'silero_model', ['v4_ru'])
        combo(voice, 'Голос Silero', 'silero_voice', ['aidar', 'baya', 'kseniya', 'xenia', 'eugene'])
        number(voice, 'Потоки CPU Silero', 'silero_threads', 1, 4)
        silero_notice = QLabel('Silero: 24000 Гц, CPU. Первая загрузка требует интернета; затем работает из кэша.\n'
                              'Веса v4_ru: CC BY-NC-SA 4.0 — некоммерческое использование, атрибуция и ShareAlike.')
        silero_notice.setWordWrap(True)
        voice.addRow(silero_notice)
        line(voice, 'Точное имя SAPI (пусто = системный)', 'sapi_voice')
        line(voice, 'Голос Edge-TTS', 'edge_voice')
        self.elevenlabs_key = QLineEdit()
        self.elevenlabs_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.elevenlabs_key.setPlaceholderText('Новый ключ; пусто = не менять')
        self.delete_elevenlabs = QCheckBox('Удалить сохранённый ключ')
        eleven_row = QHBoxLayout()
        eleven_row.addWidget(self.elevenlabs_key, 1)
        eleven_row.addWidget(self.delete_elevenlabs)
        voice.addRow('Ключ ElevenLabs', eleven_row)
        line(voice, 'Voice ID ElevenLabs', 'elevenlabs_voice_id')
        line(voice, 'Model ID ElevenLabs', 'elevenlabs_model_id')
        check = QCheckBox('Использую наушники: разрешить голосовой стоп во время ответа')
        check.setChecked(settings.headphones)
        self.inputs['headphones'] = check
        voice.addRow(check)
        notice = QLabel('Акустического эхоподавления нет. При работе через динамики\nголосовой стоп во время озвучки подавляется; ручной стоп доступен всегда.')
        notice.setWordWrap(True)
        voice.addRow(notice)
        number(voice, 'Громкость (0–1)', 'volume', 0, 1, True)
        if embedded:
            camera = tab('Камера')
            camera_note = QLabel('Камера включается только по вашей команде. Анализ экрана доступен через Gemini; поддержка изображений в LM Studio не подтверждена.')
            camera_note.setWordWrap(True); camera.addRow(camera_note)
            camera_button = QPushButton('Включить / выключить камеру')
            camera_button.clicked.connect(parent._toggle_camera); camera.addRow(camera_button)
            appearance = tab('Внешний вид')
            self.motion = QCheckBox('Облегчённое оформление без анимации')
            self.motion.setChecked(settings.reduced_motion)
            self.inputs['reduced_motion'] = self.motion
            appearance.addRow(self.motion)
            shortcuts = tab('Горячие клавиши')
            for title, text in (('F11', 'Полный экран / оконный режим'), ('Esc', 'Выход из полного экрана')):
                shortcuts.addRow(title, QLabel(text))
            self.categories.setCurrentRow(0)
        self.error = QLabel('')
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText('Сохранить')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('Отмена')
        buttons.accepted.connect(self.save_requested.emit if embedded else self.accept)
        buttons.rejected.connect(self.cancel_requested.emit if embedded else self.reject)
        layout.addWidget(buttons)
        self._refresh_devices()

    def _refresh_devices(self) -> None:
        if self.device_worker is not None and self.device_worker.is_alive():
            return
        self.refresh_devices.setEnabled(False)
        self.device_notice.setText('Получение устройств…')
        self.device_worker = MicrophoneDiscovery(self.device_signals)
        try:
            self.device_worker.start()
        except Exception:
            self._devices_failed('Не удалось запустить поиск микрофонов. Нажмите «Обновить».')

    def _devices_ready(self, devices: list) -> None:
        picker = self.inputs['input_device']
        selected = picker.currentData()
        picker.clear()
        default = next((device.name for device in devices if device.is_default), 'не найден')
        picker.addItem(f'Системный микрофон: {default}', None)
        self.available_microphones = {device.index for device in devices if device.supports_pcm16}
        for device in devices:
            picker.addItem(device.label, device.index)
            if not device.supports_pcm16:
                picker.model().item(picker.count() - 1).setEnabled(False)
        index = picker.findData(selected)
        if selected is not None and index == -1:
            picker.addItem(f'Микрофон №{selected} — недоступен', selected)
            index = picker.count() - 1
        picker.setCurrentIndex(max(0, index))
        self.refresh_devices.setEnabled(True)
        self.device_notice.setText('Выберите устройство по названию. RMS и VAD видны в консоли при включённом микрофоне.'
                                   if self.available_microphones else 'Подходящие входы не найдены. Проверьте подключение и доступ Windows к микрофону.')

    def _devices_failed(self, message: str) -> None:
        self.refresh_devices.setEnabled(True)
        self.device_notice.setText(message)

    def _lm_fingerprint(self):
        return (self.inputs['lmstudio_url'].text().strip(), self.lmstudio_token.text().strip(),
                self.delete_lmstudio.isChecked(), self.inputs['lmstudio_model'].currentText().strip())

    def _check_lmstudio(self, checked=False, *, list_only=False):
        # Snapshot widgets here; worker never accesses Qt widgets or writes credentials.
        from threading import Thread
        import asyncio
        from ..lmstudio import LMStudioProvider
        from ..providers import ProviderError
        url, token, deleting, model = self._lm_fingerprint()
        if token and deleting:
            self.lmstudio_notice.setText('Выберите замену или удаление токена, а не оба действия.')
            return
        try:
            settings = replace(self.original, lmstudio_url=url,
                               lmstudio_model=model,
                               timeout_s=min(10 if list_only else 60, self.original.timeout_s)).validate()
        except ValueError as exc:
            self.lmstudio_notice.setText(str(exc))
            return
        self._lm_check_serial += 1
        serial = self._lm_check_serial
        self._lm_check_fingerprint = self._lm_fingerprint()
        signals = self.connection_signals
        self.check_lmstudio.setEnabled(False)
        self.refresh_models.setEnabled(False)
        self.lmstudio_notice.setText('Проверка сервера LM Studio…')
        def token_loader():
            if deleting:
                raise secrets.MissingCredentialError('')
            return token or secrets.load_lmstudio_token()
        def work():
            import httpx
            async def check():
                async with httpx.AsyncClient(timeout=httpx.Timeout(settings.timeout_s, connect=3), trust_env=False) as client:
                    adapter = LMStudioProvider(settings, client, token_loader)
                    models = await adapter.models()
                    capability = 'не проверен: выберите доступный ID и повторите проверку'
                    reply = ''
                    if settings.lmstudio_model in models and not list_only:
                        try:
                            reply = await adapter.probe_text()
                        except TimeoutError:
                            raise ProviderError('LM Studio: модель не ответила за отведённое время. Загрузите её в LM Studio и увеличьте таймаут.') from None
                    return {'ids': models, 'tools': capability, 'reply': reply}
            try:
                models, message = asyncio.run(check()), ''
            except ProviderError as exc:
                models, message = [], str(exc)
            except Exception:
                models, message = [], 'LM Studio: не удалось проверить подключение.'
            try:
                signals.result.emit(serial, models, message)
            except RuntimeError:
                pass  # Dialog was closed while its read-only check was running.
        Thread(target=work, name='LMStudioConnectionCheck', daemon=True).start()

    @pyqtSlot(int, object, str)
    def _lmstudio_result(self, serial, models, message):
        if serial != self._lm_check_serial:
            return
        self.check_lmstudio.setEnabled(True)
        self.refresh_models.setEnabled(True)
        if self._lm_check_fingerprint != self._lm_fingerprint():
            self.lmstudio_notice.setText('Настройки подключения изменены. Повторите проверку.')
            return
        if message:
            self.lmstudio_notice.setText(message)
            return
        reply = models.get('reply', '')
        models = models['ids']
        picker = self.inputs['lmstudio_model']
        selected = picker.currentText()
        picker.clear()
        picker.addItems(models)
        picker.setCurrentText(selected)  # Never silently choose a different model.
        self.lmstudio_notice.setText(
            f'Подключение установлено. Моделей: {len(models)}. '
            + ('Выбранный ID найден.' if selected and selected in models else 'Выберите доступный ID модели.' if models else 'Загрузите скачанную модель в LM Studio и обновите список.')
            + ('\nОтвет модели: ' + reply if reply else '\nГенерация не проверена. Выберите ID и нажмите «Проверить подключение и модель».')
            + '\nПоддержка изображений не проверена.')

    def values(self):
        data = {}
        for key, entry in self.inputs.items():
            if isinstance(entry, QCheckBox):
                data[key] = entry.isChecked()
            elif isinstance(entry, QComboBox):
                data[key] = (entry.currentData() if key == 'input_device' or
                             (key == 'synthesizer' and entry.currentData() == 'silero') or
                             (key == 'provider' and entry.currentData() == 'lmstudio') else entry.currentText())
            elif isinstance(entry, (QSpinBox, QDoubleSpinBox)):
                data[key] = entry.value()
            elif isinstance(entry, QPlainTextEdit):
                data[key] = entry.toPlainText().strip()
            else:
                data[key] = entry.text().strip()
        if (data['input_device'] is not None and self.available_microphones is not None
                and data['input_device'] not in self.available_microphones):
            raise ValueError('Выберите доступный микрофон или системное устройство по умолчанию.')
        return replace(self.original, **data).validate()

    def save_credentials(self):
        actions = (
            ('LM Studio', self.lmstudio_token, self.delete_lmstudio,
             secrets.save_lmstudio_token, secrets.delete_lmstudio_token),
            ('Gemini', self.key, self.delete_gemini, secrets.save_gemini_key, secrets.delete_gemini_key),
            ('ElevenLabs', self.elevenlabs_key, self.delete_elevenlabs,
             secrets.save_elevenlabs_key, secrets.delete_elevenlabs_key),
        )
        # Validate every action before performing any credential mutation.
        for label, entry, delete, save_key, delete_key in actions:
            if entry.text().strip() and delete.isChecked():
                raise ValueError(f'Для ключа {label} выберите замену или удаление, а не оба действия.')
        for label, entry, delete, save_key, delete_key in actions:
            if entry.text().strip():
                # A write can take effect even if verification fails. Retire
                # cached provider state after any attempted credential change.
                self.credentials_changed = True
                save_key(entry.text().strip())  # Verified readback inside the store.
                entry.clear()
            elif delete.isChecked():
                self.credentials_changed = True
                delete_key()
                delete.setChecked(False)


class MetricsDialog(QDialog):
    def __init__(self, metrics, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Измеренные задержки')
        self.resize(700, 430)
        layout = QVBoxLayout(self)
        description = QLabel('Время в мс. Запуск выхода — переход QAudioSink в ActiveState;\nакустическая задержка динамиков и драйвера отдельно не измеряется.')
        layout.addWidget(description)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        layout.addWidget(self.text)
        self.metrics = metrics
        timer = QTimer(self)
        timer.setInterval(1000)
        timer.timeout.connect(self.refresh)
        timer.start()
        self.refresh()

    def refresh(self):
        rows = ['Этап | N | среднее | p50 | p95']
        for name, value in self.metrics.summary().items():
            rows.append(f"{METRIC_LABELS.get(name, name)} | {value['count']} | {value['mean_ms']:.1f} | {value['p50_ms']:.1f} | {value['p95_ms']:.1f}")
        self.text.setPlainText('\n'.join(rows))

