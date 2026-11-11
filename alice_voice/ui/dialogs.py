from dataclasses import replace
from PyQt6.QtCore import Qt, QTimer, QObject, QPointF, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QPlainTextEdit, QDialog, QFormLayout, QLineEdit,
    QComboBox, QSpinBox, QDoubleSpinBox, QCheckBox, QDialogButtonBox, QTabWidget,
    QListWidget, QStackedWidget, QScrollArea, QFrame,
)
from ..workers import DeviceDiscoverySignals, MicrophoneDiscovery
from ..config import OLLAMA_MODEL_PRESETS
from .. import secrets

METRIC_LABELS = {
    'audio_queue': 'Очередь кадров', 'vad': 'VAD / кадр', 'endpoint_wait': 'Ожидание тишины',
    'stt_finalize': 'Финализация STT', 'speech_end_to_text': 'Конец речи → текст',
    'llm_first': 'LLM: первый текст', 'llm_full': 'LLM: полный ответ', 'tts': 'Подготовка TTS',
    'llm_first_sentence': 'LLM: первый фрагмент', 'llm_to_first_pcm': 'LLM → первый PCM',
    'tts_first_pcm': 'TTS: первый PCM', 'provider_warmup': 'Прогрев провайдера',
    'pcm_to_playback': 'PCM → запуск выхода', 'speech_end_to_voice': 'Конец речи → запуск выхода',
    'cold_vad': 'Холодная загрузка VAD', 'cold_stt': 'Холодная загрузка Vosk',
    'whisper': 'Whisper: уточнение фразы', 'stt_feed': 'Vosk: обработка кадров',
}


class ConnectionSignals(QObject):
    result = pyqtSignal(int, object, str)


class ModelComboBox(QComboBox):
    """Keep the dropdown arrow visible under the desktop's custom Qt theme."""

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#CEE3FA'), 2))
        x, y = self.width() - 14, self.height() / 2
        painter.drawPolyline(QPointF(x - 4, y - 2), QPointF(x, y + 2), QPointF(x + 4, y - 2))


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
        functions = general or audio
        # Preserve the legacy API preference without presenting a second master switch.
        legacy = QCheckBox(self)
        legacy.setChecked(settings.desktop_commands)
        legacy.hide()
        self.inputs['desktop_commands'] = legacy
        for key, title, default in (
                ('permission_windows', 'Windows: приложения, папки, настройки, звук и медиа', True),
                ('permission_window_control', 'Окна: переключать, сворачивать и закрывать', True),
                ('permission_browser', 'Браузер: открывать сайты и искать в интернете', True),
                ('permission_screen', 'Экран: снимки, локальное распознавание и анализ', True),
                ('permission_input', 'Ввод: нажимать клавиши и управлять мышью', False),
                ('permission_games', 'Игры: наблюдать и выполнять отдельные действия', False),
                ('permission_power', 'Питание: выключать и перезагружать компьютер', False),
                ('natural_tools', 'Понимать команды, сформулированные своими словами', True)):
            check = QCheckBox(title)
            check.setChecked(getattr(settings, key, default))
            check.setAccessibleName(title)
            self.inputs[key] = check
            functions.addRow(check)
        self.permission_note = QLabel(
            'Каждое разрешение действует отдельно. Ввод, игры и питание по умолчанию выключены. '
              'Перед отправкой экрана модели показывается предпросмотр; OCR меню выполняется локально. Питание требует двух отдельных '
            'подтверждений, последнее — точной фразой; ожидание ограничено 30 секундами. '
            '«Стоп» отменяет действия и ожидание подтверждения. '
            'Свободные формулировки разбирает выбранная модель.')
        self.permission_note.setWordWrap(True)
        functions.addRow(self.permission_note)
        for key, title in (('memory_enabled', 'Постоянная память: сохранять по команде «Запомни»'),
                           ('owner_only', 'Проверка владельца для фоновой активации в «Ярмарке» (необязательно)')):
            check = QCheckBox(title)
            check.setChecked(getattr(settings, key))
            self.inputs[key] = check
            functions.addRow(check)
        line(functions, 'Каталог памяти и профиля голоса', 'data_directory')
        self.memory_facts = QPushButton('Просмотреть / исправить сохранённые факты')
        self.memory_facts.clicked.connect(self._show_memory_facts)
        functions.addRow(self.memory_facts)
        line(functions, 'Firefox.exe (пусто = найти установленный)', 'firefox_path')
        line(functions, 'Модель проверки владельца ONNX', 'speaker_model')
        number(functions, 'Порог совпадения голоса', 'speaker_threshold', .3, .95, True)
        self.inputs['speaker_threshold'].setDecimals(2)
        self.inputs['speaker_threshold'].setSingleStep(.05)
        self.inputs['speaker_threshold'].setToolTip('Порог сходства аудиопризнаков, не процент уверенности. Подбирается на записях владельца и других людей с их согласия.')
        if parent is not None and hasattr(parent, '_register_owner'):
            enroll = QPushButton('Зарегистрировать / перезаписать мой голос')
            enroll.clicked.connect(parent._register_owner)
            functions.addRow(enroll)
        memory_note = QLabel('Память: «Запомни …», «Что ты помнишь», «Забудь факт 3», «Очисти всю память».\n'
                            'До 10 последних фактов добавляются к запросу выбранной модели; облачный провайдер получает их вместе с вопросом.\n'
                            'Поиск: «Найди в интернете …», «Найди на сайте mozilla.org …».\n'
                            'Без зарегистрированного профиля режим «Только мой голос» блокирует микрофонные запросы.')
        memory_note.setWordWrap(True)
        functions.addRow(memory_note)
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
        recognition = QComboBox()
        recognition.addItem('Vosk — быстро', 'vosk')
        recognition.addItem('Vosk + Whisper — уточнение фраз', 'hybrid')
        recognition.setCurrentIndex(recognition.findData(settings.stt_backend))
        self.inputs['stt_backend'] = recognition
        audio.addRow('Распознавание речи', recognition)
        line(audio, 'Каталог локальной модели Whisper', 'whisper_model')
        number(audio, 'Порог начала речи', 'threshold', .01, .99, True)
        self.inputs['threshold'].setToolTip('Вероятность речи Silero, не громкость. Выше — строже, но тихую речь легче пропустить.')
        number(audio, 'Минимум речи, мс', 'min_speech_ms', 0, 1000)
        self.inputs['min_speech_ms'].setToolTip('По умолчанию 96 мс речи для коротких слов; щелчки короче этого порога отбрасываются. 0 отключает фильтр. Удержание кнопки не использует этот порог.')
        number(audio, 'Тишина, мс', 'silence_ms', 32, 3000)
        number(audio, 'Преролл, мс', 'preroll_ms', 0, 1000)
        number(audio, 'Максимальная фраза, с', 'max_phrase_s', 1, 120, True)
        if llm is None: llm = tab('Модель')
        combo(llm, 'Провайдер', 'provider', ['ollama', 'gemini'])
        self.inputs['provider'].addItem('LM Studio · локально', 'lmstudio')
        if settings.provider == 'lmstudio':
            self.inputs['provider'].setCurrentIndex(self.inputs['provider'].findData('lmstudio'))
        line(llm, 'URL Ollama', 'ollama_url')
        ollama_model = ModelComboBox()
        ollama_model.setEditable(True)
        ollama_model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        ollama_model.setAccessibleName('Модель Ollama')
        ollama_model.addItems(list(dict.fromkeys((*OLLAMA_MODEL_PRESETS, settings.ollama_model))))
        ollama_model.setCurrentText(settings.ollama_model)
        ollama_model.setToolTip('Выберите модель или введите своё имя. Модель должна быть установлена в Ollama.')
        self.inputs['ollama_model'] = ollama_model
        ollama_row = QHBoxLayout()
        ollama_row.addWidget(ollama_model, 1)
        self.refresh_ollama = QPushButton('Обновить')
        self.refresh_ollama.setAccessibleName('Обновить список установленных моделей Ollama')
        self.refresh_ollama.clicked.connect(self._refresh_ollama_models)
        ollama_row.addWidget(self.refresh_ollama)
        llm.addRow('Модель Ollama', ollama_row)
        self.ollama_notice = QLabel('Выберите модель из списка ▾. «Обновить» покажет модели, установленные в Ollama.')
        self.ollama_notice.setWordWrap(True)
        llm.addRow(self.ollama_notice)
        self.ollama_signals = ConnectionSignals(self)
        self.ollama_signals.result.connect(self._ollama_result, Qt.ConnectionType.QueuedConnection)
        self._ollama_serial = 0
        self._ollama_url = None
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
        fair=tab('Ярмарка')
        self.fair_device=QComboBox()
        self.fair_device.setModel(device.model())
        self.fair_device.setCurrentIndex(device.currentIndex())
        self.fair_device.currentIndexChanged.connect(device.setCurrentIndex)
        device.currentIndexChanged.connect(self.fair_device.setCurrentIndex)
        fair.addRow('Микрофон',self.fair_device)
        owner_check=QCheckBox('Проверять голос владельца при активации по имени')
        owner_check.setChecked(settings.owner_only)
        owner_check.toggled.connect(self.inputs['owner_only'].setChecked)
        self.inputs['owner_only'].toggled.connect(owner_check.setChecked)
        fair.addRow(owner_check)
        for key,title in (('fair_mode','Режим «Ярмарка»: принимать только активированные команды'),
                          ('wake_enabled','Активация по имени'),('fair_continue','Продолжать разговор после ответа (короткое окно)')):
            entry=QCheckBox(title);entry.setChecked(getattr(settings,key));self.inputs[key]=entry;fair.addRow(entry)
        noise=QComboBox()
        for title,value in (('Выключено','off'),('Обычный · RNNoise','normal'),('Ярмарка · RNNoise и ограниченная активация','fair')):noise.addItem(title,value)
        noise.setCurrentIndex(noise.findData(settings.noise_mode));self.inputs['noise_mode']=noise
        fair.addRow('Шумоподавление',noise)
        line(fair,'Имя помощника','wake_name')
        number(fair,'Окно команды после имени, с','wake_window_s',2,15,True)
        number(fair,'Чувствительность активации (порог VAD)','fair_threshold',.1,.95,True)
        self.inputs['fair_threshold'].setToolTip('Выше — строже фильтр речи. Этот порог не определяет личность говорящего.')
        number(fair,'Минимальное качество STT (не вероятность личности)','fair_stt_confidence',.1,.99,True)
        line(fair,'Удерживать клавишу (например Ctrl+F8)','ptt_hotkey')
        note=QLabel('Для демонстрации: удерживайте кнопку или клавишу и говорите рядом с микрофоном. '
            'Микрофон выбирается в «Аудио / STT», проверка владельца — в общих настройках. '
            'Удержание — явное разрешение на одну команду без проверки личности. '
            'RNNoise не выделяет ваш голос из разговоров. Автоусиления нет. '
            'С колонками: при озвучке команды выключены; удержание сначала останавливает ответ. Это не AEC. '
            'Порог требует проверки на реальных записях; при перекрывающейся речи говорите по одному.')
        note.setWordWrap(True);fair.addRow(note)
        self.microphone_test=QPushButton('Проверка микрофона: исходный / обработанный звук')
        self.microphone_test.clicked.connect(self._test_fair_microphone);fair.addRow(self.microphone_test)
        if parent is not None and hasattr(parent,'_register_owner'):
            enroll=QPushButton('Зарегистрировать / перезаписать голос в тишине')
            enroll.clicked.connect(parent._register_owner);fair.addRow(enroll)
            remove=QPushButton('Удалить локальный голосовой профиль')
            remove.clicked.connect(parent._delete_owner);fair.addRow(remove)
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
        QTimer.singleShot(0, self._refresh_ollama_models)

    def _show_memory_facts(self):
        from pathlib import Path
        from ..memory import FactMemory
        from .memory_dialog import MemoryFactsDialog
        directory = self.inputs['data_directory'].text().strip()
        if not directory:
            self.error.setText('Укажите каталог памяти.')
            return
        dialog = MemoryFactsDialog(FactMemory(Path(directory) / 'memory.sqlite3'), self)
        dialog.exec()

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

    def _refresh_ollama_models(self, checked=False):
        from threading import Thread
        from urllib.parse import urlsplit

        if not self.refresh_ollama.isEnabled():
            return
        url = self.inputs['ollama_url'].text().strip().rstrip('/')
        try:
            parts = urlsplit(url)
            if (parts.scheme not in ('http', 'https') or not parts.hostname
                    or parts.username or parts.password or parts.query or parts.fragment):
                raise ValueError
            parts.port
        except ValueError:
            self.ollama_notice.setText('Введите корректный URL Ollama: http://127.0.0.1:11434')
            return
        self._ollama_serial += 1
        serial = self._ollama_serial
        self._ollama_url = url
        signals = self.ollama_signals
        self.refresh_ollama.setEnabled(False)
        self.ollama_notice.setText('Получение установленных моделей Ollama…')

        def work():
            import httpx
            try:
                with httpx.Client(timeout=5, trust_env=False) as client:
                    response = client.get(url + '/api/tags')
                    response.raise_for_status()
                    names = response.json()['models']
                    if not isinstance(names, list):
                        raise ValueError
                    models = []
                    for model in names:
                        name = model.get('name') if isinstance(model, dict) else None
                        if not isinstance(name, str) or not name.strip():
                            raise ValueError
                        models.append(name.strip())
                    models = list(dict.fromkeys(models))
                message = ''
            except Exception:
                models, message = [], 'Не удалось получить модели. Проверьте URL и запустите Ollama, затем нажмите «Обновить».'
            try:
                signals.result.emit(serial, models, message)
            except RuntimeError:
                pass  # The settings page was destroyed during the request.

        Thread(target=work, name='OllamaModelDiscovery', daemon=True).start()

    @pyqtSlot(int, object, str)
    def _ollama_result(self, serial, models, message):
        if serial != self._ollama_serial:
            return
        self.refresh_ollama.setEnabled(True)
        if self.inputs['ollama_url'].text().strip().rstrip('/') != self._ollama_url:
            self.ollama_notice.setText('URL Ollama изменён. Нажмите «Обновить».')
            return
        if message:
            self.ollama_notice.setText(message)
            return
        picker = self.inputs['ollama_model']
        selected = picker.currentText()
        picker.clear()
        picker.addItems(models)
        picker.setCurrentText(selected)
        self.ollama_notice.setText(
            f'Установлено моделей: {len(models)}. '
            + ('Выберите модель из списка ▾.' if models else 'Установите модель в Ollama и обновите список.')
            + (' Текущая модель отсутствует на сервере.' if selected and selected not in models else ''))

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

    def _test_fair_microphone(self):
        from .microphone_test import MicrophoneTestDialog
        try:
            settings=self.values()
        except ValueError as error:
            self.error.setText(str(error));return
        owner=self.parentWidget()
        dialog=MicrophoneTestDialog(settings,getattr(owner,'controller',None),self)
        dialog.exec()

    def values(self):
        data = {}
        for key, entry in self.inputs.items():
            if isinstance(entry, QCheckBox):
                data[key] = entry.isChecked()
            elif isinstance(entry, QComboBox):
                data[key] = (entry.currentData() if key in ('input_device','noise_mode','stt_backend') or
                             (key == 'synthesizer' and entry.currentData() == 'silero') or
                             (key == 'provider' and entry.currentData() == 'lmstudio') else entry.currentText())
                if key == 'ollama_model':
                    data[key] = data[key].strip()
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

