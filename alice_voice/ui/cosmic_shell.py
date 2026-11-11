"""Cosmic desktop shell; session ownership stays in MainWindow's controller."""
from time import perf_counter
from PyQt6.QtCore import Qt, QPoint, QRect
from PyQt6.QtGui import QFontMetrics
from PyQt6.QtWidgets import (QWidget,QFrame,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,
    QComboBox,QStackedWidget,QPlainTextEdit,QMenu,QDialog,QScrollArea,QSizePolicy,QCheckBox,QLineEdit)
from PyQt6.QtMultimedia import QCamera,QMediaCaptureSession,QMediaDevices,QVideoSink
from .cosmic_widgets import CosmicOrb,AudioWave,IconButton,VolumeControl
from .widgets import VoiceLogo,StatusLabel,WindowControlButton

from .cosmic_theme import STYLE, SIDEBAR_WIDTH, COMPACT_SIDEBAR_WIDTH, ORB_MAX_SIZE


class CosmicShell:
    def _build_shell(self):
        self.current_page='voice'; self.sidebar_collapsed=False; self.camera=None; self.camera_session=None; self.camera_sink=None; self.mini=None
        self._smoothed_level=0.; self._history_snapshot=None
        root=QHBoxLayout(self.background); root.setContentsMargins(0,0,0,0); root.setSpacing(0)
        self.sidebar=QWidget(); self.sidebar.setFixedWidth(SIDEBAR_WIDTH)
        side=QVBoxLayout(self.sidebar); side.setContentsMargins(14,20,14,24); side.setSpacing(12)
        brand=QHBoxLayout(); self.logo=VoiceLogo(); self.logo.setFixedSize(34,38)
        self.brand=QLabel('Alice Voice'); self.brand.setObjectName('brand')
        self.collapse_btn=IconButton('sidebar','Свернуть боковую панель',self._collapse_sidebar); self.collapse_btn.setFixedSize(32,38)
        brand.addWidget(self.logo); brand.addWidget(self.brand,1); brand.addWidget(self.collapse_btn); side.addLayout(brand)
        self.segments=QFrame(); self.segments.setObjectName('segments'); segments=QHBoxLayout(self.segments); segments.setContentsMargins(4,3,4,3)
        self.voice_mode=self._button('Голос',lambda:self.select_page('voice')); self.work_mode=self._button('Работа',lambda:self.select_page('work'))
        for b in (self.voice_mode,self.work_mode): b.setCheckable(True); b.setProperty('segment',True); segments.addWidget(b)
        side.addWidget(self.segments); side.addSpacing(8)
        self.nav_buttons={}; self.nav_icons={}
        items=(('voice','Голосовой центр','Микрофон и последняя реплика','voice'),('dialog','Диалог','Переписка и история бесед','dialog'),('work','Рабочая панель','Команды и последние действия','work'),('mini','Мини-ассистент','Компактное окно','mini'))
        self.nav_text={}
        for key,title,subtitle,icon in items:
            line=QHBoxLayout(); line.setContentsMargins(0,0,0,0); line.setSpacing(0)
            glyph=IconButton(icon,title,lambda checked=False,k=key:self.select_page(k)); glyph.setFixedSize(40,48)
            b=self._button('',lambda checked=False,k=key:self.select_page(k)); b.setCheckable(True); b.setProperty('nav',True); b.setFixedHeight(48)
            labels=QVBoxLayout(b); labels.setContentsMargins(12,8,6,8); labels.setSpacing(4)
            label=QLabel(title); label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents); label.setStyleSheet('font-size: 15px;'); labels.addWidget(label)
            b.setAccessibleName(title)
            b.setToolTip(f'{title} — {subtitle}'); b.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Fixed)
            self.nav_buttons[key]=b; self.nav_icons[key]=glyph; self.nav_text[key]=(title,subtitle)
            line.addWidget(glyph); line.addWidget(b,1); side.addLayout(line)
        side.addStretch(1)
        settings_row=QHBoxLayout(); settings_row.setSpacing(0); self.settings_icon=IconButton('settings','Настройки',self._show_settings); self.settings_icon.setFixedSize(40,48); settings_row.addWidget(self.settings_icon)
        self.settings_btn=self._button('Настройки',self._show_settings); self.settings_btn.setCheckable(True); self.settings_btn.setProperty('nav',True); self.settings_btn.setMinimumHeight(48); self.settings_btn.setStyleSheet('text-align: left; padding-left: 12px;'); settings_row.addWidget(self.settings_btn,1); side.addLayout(settings_row)
        root.addWidget(self.sidebar)
        self.main_area=QWidget(); main=QVBoxLayout(self.main_area); main.setContentsMargins(24,12,24,18); main.setSpacing(4)
        self.header=QWidget(); header=self.header_row=QHBoxLayout(self.header); header.setContentsMargins(0,0,0,0)
        self.section_title=QLabel('Голосовой помощник'); self.section_title.setObjectName('sectionTitle'); header.addWidget(self.section_title)
        self.provider_combo=QComboBox(); self.provider_combo.setAccessibleName('Провайдер модели')
        for title,key in (('Ollama · локально','ollama'),('Gemini · онлайн','gemini'),('LM Studio · локально','lmstudio')): self.provider_combo.addItem(title,key)
        self.provider_combo.currentIndexChanged.connect(self._provider_changed); header.addWidget(self.provider_combo)
        self.model_combo=QComboBox(); self.model_combo.setEditable(True); self.model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert); self.model_combo.setAccessibleName('Модель LLM'); self.model_combo.setMaximumWidth(190)
        self.model_combo.activated.connect(self._model_changed); self.model_combo.lineEdit().editingFinished.connect(self._model_changed); header.addWidget(self.model_combo)
        header.addStretch(); self.clock=QLabel(); self.clock.setObjectName('clock'); self.clock.setMinimumWidth(80); self.clock.setAlignment(Qt.AlignmentFlag.AlignCenter); self.clock.setToolTip('Локальное время · часы и минуты'); header.addWidget(self.clock)
        self.minimize_btn=WindowControlButton('minimize'); self.maximize_btn=WindowControlButton('maximize'); self.close_btn=WindowControlButton('close')
        self.minimize_btn.clicked.connect(self.showMinimized); self.maximize_btn.clicked.connect(self._toggle_maximized); self.close_btn.clicked.connect(self.close)
        for b in (self.minimize_btn,self.maximize_btn,self.close_btn): header.addWidget(b)
        main.addWidget(self.header)
        control_row=QHBoxLayout()
        self.fair_badge=QPushButton('ЯРМАРКА · Выключить')
        self.fair_badge.setAccessibleName('Выключить режим Ярмарка и слушать без имени')
        self.fair_badge.setToolTip('Перейти к обычному прослушиванию без имени и удержания кнопки')
        self.fair_badge.clicked.connect(lambda: self._fair_protocol(False))
        self.fair_badge.setStyleSheet('color: #FFD3A1; background: #5A2B0D; border: 1px solid #FF9B38; border-radius: 10px; padding: 6px 10px; font-weight: 600;')
        self.fair_badge.hide()
        control_row.addWidget(self.fair_badge)
        self.control_status=QLabel(); self.control_status.setTextFormat(Qt.TextFormat.PlainText); self.control_status.setWordWrap(True); self.control_status.setAccessibleName('Состояние управления компьютером')
        control_row.addWidget(self.control_status,1)
        self.ptt_button=QPushButton('Удерживать, чтобы говорить')
        self.ptt_button.setAccessibleName('Удерживать, чтобы говорить')
        self.ptt_button.pressed.connect(self.controller.begin_ptt)
        self.ptt_button.released.connect(self.controller.end_ptt)
        control_row.addWidget(self.ptt_button)
        self.global_stop_btn=self._button('Стоп',self._stop_actions); self.global_stop_btn.setAccessibleName('Остановить все действия'); control_row.addWidget(self.global_stop_btn)
        main.addLayout(control_row)
        self.pages=QStackedWidget(); main.addWidget(self.pages,1)
        self.content=QWidget(); hero=QVBoxLayout(self.content); hero.setContentsMargins(12,0,12,0); hero.setSpacing(7)
        hero.addStretch(2)
        self.orb=CosmicOrb(); self.orb.setFixedSize(340,340); self.voice_stage=self.orb
        hero.addWidget(self.orb,alignment=Qt.AlignmentFlag.AlignCenter)
        self.state_label=StatusLabel(); self.state_label.setObjectName('stateLabel'); self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter); self.state_label.setMinimumHeight(48); self.state_label.setWordWrap(True); self.state_label.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred); hero.addWidget(self.state_label)
        self.subtitle=QLabel('Говорите — я рядом'); self.subtitle.setObjectName('subtitle'); self.subtitle.setWordWrap(True); self.subtitle.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred); self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter); hero.addWidget(self.subtitle)
        self.user_label=QLabel(); self.user_label.setObjectName('userText'); self.user_label.setTextFormat(Qt.TextFormat.PlainText); self.user_label.setWordWrap(True); self.user_label.setMaximumHeight(44); self.user_label.setAlignment(Qt.AlignmentFlag.AlignCenter); hero.addWidget(self.user_label)
        self.wave=AudioWave(); hero.addWidget(self.wave,alignment=Qt.AlignmentFlag.AlignCenter)
        self.retry_btn=self._button('Повторить подключение',self._retry_connection); hero.addWidget(self.retry_btn,alignment=Qt.AlignmentFlag.AlignCenter); self.retry_btn.hide()
        hero.addSpacing(0)
        self.control_area=QWidget(); self.control_area.setFixedSize(316,144)
        dock=self.dock=QFrame(self.control_area); dock.setObjectName('dock'); dock.setGeometry(0,78,316,66)
        self.volume_control=VolumeControl(self.control_area); self.volume_control.setGeometry(8,0,210,144)
        self.volume=self.volume_control.slider; self.volume.valueChanged.connect(self._volume_changed)
        self.mic_btn=IconButton('mic','Включить микрофон',self._toggle_microphone,self.control_area); self.mic_btn.setFixedSize(52,52); self.mic_btn.move(88,86)
        self.camera_btn=IconButton('camera','Включить камеру',self._toggle_camera,self.control_area); self.camera_btn.move(170,88)
        self.more_btn=IconButton('more','Дополнительные действия',self._open_more,self.control_area); self.more_btn.move(248,88)
        hero.addWidget(self.control_area,alignment=Qt.AlignmentFlag.AlignCenter)
        self.mic_status=QLabel(); self.mic_status.setObjectName('muted'); self.mic_status.setAlignment(Qt.AlignmentFlag.AlignCenter); self.mic_status.setWordWrap(True); hero.addWidget(self.mic_status)
        self.notice=QLabel(); self.notice.setTextFormat(Qt.TextFormat.PlainText); self.notice.setWordWrap(True); self.notice.setObjectName('muted'); self.notice.setAlignment(Qt.AlignmentFlag.AlignCenter); hero.addWidget(self.notice)
        hero.addStretch(1)
        self.pages.addWidget(self.content)
        self.dialog_page=QWidget(); dialog=QVBoxLayout(self.dialog_page)
        dialog.addWidget(QLabel('Переписка текущего сеанса'))
        self.transcript=QPlainTextEdit(); self.transcript.setReadOnly(True); dialog.addWidget(self.transcript,1)
        self.answer_text=QPlainTextEdit(); self.answer_text.setReadOnly(True); self.answer_text.setPlaceholderText('Текущий ответ Алисы'); self.answer_text.setMaximumHeight(150); dialog.addWidget(self.answer_text)
        self.pages.addWidget(self.dialog_page)
        self.scroll=QScrollArea(); self.scroll.setWidgetResizable(True); self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        work=QWidget(); work.setObjectName('scrollContent'); actions=QVBoxLayout(work); actions.setSpacing(16)
        heading=QLabel('Команды и последние действия'); heading.setObjectName('sectionTitle'); actions.addWidget(heading)
        self.work_control_status=QLabel(); self.work_control_status.setTextFormat(Qt.TextFormat.PlainText); self.work_control_status.setWordWrap(True); actions.addWidget(self.work_control_status)
        row=QHBoxLayout(); self.stop_btn=self._button('Стоп — отменить действия',self._stop_actions); self.pause_btn=self._button('Пауза',self.controller.toggle_pause)
        row.addWidget(self.stop_btn); row.addWidget(self.pause_btn); row.addWidget(self._button('Задержки',self._show_metrics)); actions.addLayout(row)
        command_label=QLabel('Команда или подтверждение'); actions.addWidget(command_label)
        command_row=QHBoxLayout(); self.command_input=QLineEdit(); self.command_input.setAccessibleName('Команда или подтверждение'); self.command_input.setPlaceholderText('Например: открой Блокнот'); command_label.setBuddy(self.command_input)
        self.command_input.returnPressed.connect(self._submit_command); command_row.addWidget(self.command_input,1)
        self.command_send=self._button('Отправить',self._submit_command); command_row.addWidget(self.command_send); actions.addLayout(command_row)
        actions.addWidget(QLabel('Последние действия'))
        self.action_log=QPlainTextEdit(); self.action_log.setReadOnly(True); self.action_log.setAccessibleName('Журнал последних действий'); self.action_log.setPlaceholderText('Действий пока нет'); self.action_log.setMaximumHeight(180); actions.addWidget(self.action_log)
        self.screen_btn=self._button('Посмотреть экран',lambda:self.controller.request_screen_preview()); self.screen_btn.setAccessibleName('Посмотреть экран'); actions.addWidget(self.screen_btn)
        self.voice_combo=QComboBox(); self.voice_combo.setAccessibleName('Синтез речи')
        for title,key in (('Windows SAPI · локально','sapi'),('Edge-TTS · онлайн','edge'),('ElevenLabs (онлайн)','elevenlabs'),('Silero · локально','silero')): self.voice_combo.addItem(title,key)
        self.voice_combo.currentIndexChanged.connect(self._voice_changed); actions.addWidget(QLabel('Голос ассистента')); actions.addWidget(self.voice_combo)
        self.reduced_motion=QCheckBox('Облегчённое оформление без анимации'); self.reduced_motion.toggled.connect(lambda checked:self.set_motion_enabled(not checked)); actions.addWidget(self.reduced_motion)
        self.history_label=QLabel('Пока нет завершённых ответов'); self.history_label.setWordWrap(True); actions.addWidget(self.history_label); actions.addStretch()
        self.scroll.setWidget(work); self.pages.addWidget(self.scroll)
        root.addWidget(self.main_area,1)
        self.select_page('voice')
        self.notice_banner=QLabel(self.main_area); self.notice_banner.setObjectName('muted'); self.notice_banner.setTextFormat(Qt.TextFormat.PlainText); self.notice_banner.setWordWrap(True); self.notice_banner.hide(); main.addWidget(self.notice_banner)

    def select_page(self,page):
        if page=='mini': self._open_mini(); return
        self.current_page=page
        self.pages.setCurrentIndex({'voice':0,'dialog':1,'work':2,'settings':3}[page])
        self.section_title.setText({'voice':'Голосовой помощник','dialog':'Диалог','work':'Рабочая панель','settings':'Настройки'}[page])
        for key,b in self.nav_buttons.items():
            b.setChecked(key==page)
            self.nav_icons[key].active=key==page
            self.nav_icons[key].update()
        self.voice_mode.setChecked(page=='voice'); self.work_mode.setChecked(page=='work')
        self.settings_btn.setChecked(page=='settings'); self.settings_icon.active=page=='settings'; self.settings_icon.update()
        self.dirty=True

    def _show_dialog(self): self.select_page('dialog')
    def _home(self):
        from time import perf_counter
        self.select_page('voice')
        self.entrance.start(perf_counter(),enabled=self.motion_enabled)
    def _apply_theme(self): self.setStyleSheet(STYLE); self.dirty=True

    def _collapse_sidebar(self):
        self.sidebar_collapsed=not self.sidebar_collapsed; self._adapt_layout()

    def _adapt_layout(self):
        if not hasattr(self,'sidebar'): return
        self.dirty=True
        compact=self.sidebar_collapsed or self.width()<950
        self.sidebar.setFixedWidth(COMPACT_SIDEBAR_WIDTH if compact else SIDEBAR_WIDTH)
        self.background.sidebar_width=self.sidebar.width()
        self.brand.setVisible(not compact); self.logo.setVisible(not compact); self.segments.setVisible(not compact)
        for key,b in self.nav_buttons.items(): b.setVisible(not compact)
        self.settings_btn.setVisible(not compact)
        self.settings_btn.setToolTip('Настройки')
        self.model_combo.setVisible(self.width()>1100)
        self.section_title.setVisible(self.width()>600)
        self.provider_combo.setMaximumWidth(180 if self.width()>700 else 140)
        short=self.height()<760
        extra=(30 if self.controller.recognized or self.controller.partial else 0)+(30 if self.notice_text else 0)+(35 if self._mode()[0]=='error' else 0)
        size=max(80,min(ORB_MAX_SIZE,self.height()-415-extra)) if short else ORB_MAX_SIZE
        self.orb.setFixedSize(size,size)
        self.wave.setFixedWidth(min(350,max(180,self.main_area.width()-50)))
        self.user_label.setMaximumWidth(max(200,self.main_area.width()-60))
        self.state_label.setStyleSheet('font-size: 26px;' if short else '')
        self.content.layout().setSpacing(4 if short else 7)
        self.wave.setFixedHeight(36 if short else 56)
        self.subtitle.setStyleSheet('font-size: 15px;' if short else '')
        self.control_area.setFixedHeight(104 if short else 144)
        self.dock.move(0,38 if short else 78)
        self.volume_control.set_compact(short)
        self.mic_btn.move(88,46 if short else 86)
        self.camera_btn.move(170,48 if short else 88)
        self.more_btn.move(248,48 if short else 88)
        self.notice.setMaximumHeight(36 if short else 44)
        self.mic_status.setStyleSheet('font-size: 11px;' if short else '')
        self.background.update()

    def _render_cosmic(self,mode):
        c=self.controller
        geometry_key=(self.width(),self.height(),bool(c.recognized or c.partial),bool(self.notice_text),mode=='error')
        if getattr(self,'_voice_geometry_key',None)!=geometry_key:
            self._voice_geometry_key=geometry_key
            self._adapt_layout()
        self.subtitle.setVisible(not(self.height()<760 and mode=='error'))
        self.notice.setVisible(bool(self.notice_text)); self.notice.setToolTip(self.notice_text)
        self.notice_banner.setText(self.notice_text); self.notice_banner.setVisible(self.current_page!='voice' and bool(self.notice_text))
        self.mic_status.setText('Нажмите на микрофон, чтобы приостановить' if c.capture is not None else 'Нажмите на микрофон, чтобы начать разговор')

        self.retry_btn.setVisible(mode=='error')
        self.retry_btn.setText('Настройки подключения' if c.last_error else 'Повторить подключение')
        self.subtitle.setText({'off':'Я рядом — начнём, когда будете готовы','thinking':'Обдумываю ваш запрос','speaking':'Слушайте — я отвечаю','error':'Проверьте устройство и повторите подключение'}.get(mode,'Говорите — я рядом'))
        snapshot=(tuple(c.completed_turns),c.recognized,c.answer)
        if snapshot!=self._history_snapshot:
            self._history_snapshot=snapshot
            history='\n\n'.join(f'Вы: {u}\nАлиса: {a}' for u,a in c.completed_turns)
            if c.recognized and (not c.completed_turns or c.completed_turns[-1]!=(c.recognized,c.answer)):
                history+=f'\n\nВы: {c.recognized}\nАлиса: {c.answer}'
            self.transcript.setPlainText(history.strip() or 'Начните разговор — здесь появится переписка.')

    def _sync_user_replica(self):
        """Measure wrapping only after text, width, font or display scale changes."""
        text=self.controller.partial or self.controller.recognized
        width=max(120,self.user_label.width())
        font=self.user_label.font()
        key=(text,width,font.toString(),self.user_label.logicalDpiX(),
             self.user_label.logicalDpiY(),self.user_label.devicePixelRatioF())
        if getattr(self,'_replica_fit_key',None)==key:
            return
        self._replica_fit_key=key
        self.user_label.setVisible(bool(text))
        self.user_label.setToolTip(text)
        if not text:
            self.user_label.setText('')
            return
        metrics=QFontMetrics(font)
        excerpt=text[:240]
        while excerpt and metrics.boundingRect(QRect(0,0,width,1000),Qt.TextFlag.TextWordWrap,f'Последняя реплика: «{excerpt}…»').height()>metrics.lineSpacing()*2:
            excerpt=excerpt[:-1]
        self.user_label.setText(f'Последняя реплика: «{excerpt}{"…" if len(excerpt)<len(text) else ""}»')

    def _render_voice_frame(self,mode):
        """Advance visual feedback without rebuilding transcript and controls."""
        c=self.controller
        self._sync_user_replica()
        phase=perf_counter() if self.motion_enabled else 0.
        target=(min(1.,c.level*14) if mode=='listening' and c.capture is not None else c.playback.output_level() if mode=='speaking' else 0.)
        self._smoothed_level += (target-self._smoothed_level)*(.55 if target>self._smoothed_level else .3)
        if abs(self._smoothed_level)<.001: self._smoothed_level=0.
        key=(mode,phase,self._smoothed_level,c.capture is not None)
        if getattr(self,'_voice_frame_key',None)!=key:
            self._voice_frame_key=key
            self.mic_btn.mode=self.voice_stage.mode=mode
            self.mic_btn.phase=self.voice_stage.phase=phase
            self.orb.level=self.wave.level=self._smoothed_level
            self.wave.mode=mode; self.wave.phase=phase
            self.mic_btn.active=c.capture is not None
            self.mic_btn.update(); self.voice_stage.update(); self.wave.update()
        if self.mini is not None and self.mini.isVisible() and not self.mini.isMinimized():
            self.mini.sync(mode,self.state_label.text(),self.orb.level,self.orb.phase)

    def _retry_connection(self):
        if self.controller.last_error:
            self._show_settings()
        else:
            self._toggle_microphone()

    def _open_more(self):
        menu=QMenu(self)
        action=menu.addAction('Посмотреть экран',lambda:self.controller.request_screen_preview()); action.setEnabled(self.screen_btn.isEnabled())
        menu.addAction('Открыть расшифровку',self._show_dialog)
        menu.addAction('Устройства ввода и вывода',self._show_settings)
        menu.addAction('Настройки голоса',self._show_settings)
        menu.addSeparator(); menu.addAction('Мини-ассистент',self._open_mini)
        action=menu.addAction('Облегчённое оформление'); action.setCheckable(True); action.setChecked(not self.motion_enabled); action.toggled.connect(lambda checked:self.set_motion_enabled(not checked))
        menu.adjustSize(); menu.exec(self.more_btn.mapToGlobal(QPoint(self.more_btn.width()-menu.sizeHint().width(),-menu.sizeHint().height()-8)))

    def _open_mini(self):
        if self.mini is None:
            from .mini_assistant import MiniAssistant
            self.mini=MiniAssistant(self)
            available=self.screen().availableGeometry()
            self.mini.move(available.right()-self.mini.width()-24,available.top()+80)
        self.mini.sync(self._mode()[0],self._mode()[1],self.orb.level,self.orb.phase)
        self.mini.showNormal(); self.mini.raise_(); self.mini.activateWindow(); self.dirty=True

    def _restore_main(self):
        if self.mini is not None: self.mini.hide()
        if self.isMinimized(): self.showNormal()
        else: self.show()
        self.raise_(); self.activateWindow()

    def _toggle_camera(self):
        if self.camera is not None:
            self._stop_camera(); return
        devices=QMediaDevices.videoInputs()
        if not devices:
            self._notice('Камера недоступна. Проверьте подключение и разрешения Windows.'); return
        try:
            self.camera=QCamera(devices[0],self)
            self.camera_session=QMediaCaptureSession(self); self.camera_sink=QVideoSink(self)
            self.camera_session.setVideoSink(self.camera_sink); self.camera_session.setCamera(self.camera)
            self.camera.activeChanged.connect(self._camera_active)
            self.camera.errorOccurred.connect(self._camera_error)
            self.camera.start()
        except (RuntimeError,PermissionError) as error:
            self._stop_camera(); self._notice(f'Не удалось включить камеру: {error}')

    def _camera_active(self,active):
        self.camera_btn.active=active; self.camera_btn.update()
        self.camera_btn.setAccessibleName('Выключить камеру' if active else 'Включить камеру')
        self.camera_btn.setToolTip(self.camera_btn.accessibleName())
        self._notice('Камера включена' if active else 'Камера выключена')

    def _camera_error(self,*args):
        self._stop_camera(); self._notice('Камера недоступна. Проверьте разрешения Windows.')

    def _stop_camera(self):
        camera,self.camera=self.camera,None
        if camera is not None:
            camera.stop(); camera.deleteLater()
        if self.camera_session is not None:
            self.camera_session.setCamera(None); self.camera_session.setVideoSink(None); self.camera_session.deleteLater(); self.camera_session=None
        if self.camera_sink is not None: self.camera_sink.deleteLater(); self.camera_sink=None
        self._camera_active(False)
