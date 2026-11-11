from time import perf_counter
from collections import deque
from dataclasses import replace
import logging
from uuid import uuid4
from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal

from .metrics import LatencyMetrics
from .playback import PCMPlayback, EchoGuard
from .recognition import is_stop_command
from .session import SessionGuard
from .speech import MAX_PCM_BYTES
from .workers import WorkerSignals, CaptureWorker, DialogRuntime
from .utils.disfluency_filter import DisfluencyFilter
from .utils.vision import is_screen_request, MAX_IMAGE_BYTES
from .utils.barge_in import PlaybackReference, is_response_stop, matches_assistant


class VoiceSessionController(QObject):
    model_change_requested = pyqtSignal(str)
    changed = pyqtSignal()
    notification = pyqtSignal(str)
    closed = pyqtSignal()
    settings_applied = pyqtSignal(object)
    turn_finished = pyqtSignal(str, str)
    screen_preview_requested = pyqtSignal(str, str)
    confirmation_visible = pyqtSignal(object)
    fair_protocol_requested = pyqtSignal(bool)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        from .conversation import ConversationContext
        self.conversation = ConversationContext(settings.history_turns)
        self._first_text_recorded = False
        self._last_pcm_finished = None
        from .fair_mode import ActivationGate
        self.fair_gate=ActivationGate(settings)
        self.fair_state=''
        self.fair_block_until=0.
        self.ptt_down=False
        self.ptt_tokens={}
        self.last_fair_notice=0.
        self.input_speech=False
        self.testing_microphone=False
        self.local_notice=False
        self.disfluency_filter = DisfluencyFilter()
        self.metrics = LatencyMetrics()
        self.signals = WorkerSignals(self)
        self.signals.model_selected.connect(self._model_selected, Qt.ConnectionType.QueuedConnection)
        self.playback = PCMPlayback(self)
        self.echo = EchoGuard()
        self.playback_reference = PlaybackReference()
        self.voice_interruptions = {}  # In-progress utterances; bounded below.
        self.consumed_controls = deque(maxlen=256)
        self.guard = SessionGuard()
        self.capture = None
        self.runtime = None
        self.epoch = 0
        self.mic_state = 'off'
        self.stage = 'idle'
        self.last_error = ''
        self.action_log = []
        self.control_summary = ''
        self.automation_active = False
        self.power_pending = False
        self.pending_text = None
        self.partial = self.recognized = self.answer = ''
        self.level = 0
        self.closing = False
        self.speech_end = self.pcm_ready = 0
        self.busy_start = 0
        self.busy_intervals = []
        self.accept_after = 0
        self.pcm_queue = deque()
        self.current_chunk = None
        self.pcm_bytes = 0
        self.chunk_sequences = set()
        self.prefetched_sequences = set()
        self.response_paused = False
        self.output_started = False
        self.first_voice_recorded = False
        self.producer_done = False
        self.reconfiguring = False
        self.pending_settings = None
        self.resume_after_reconfigure = False
        self.completed_turns = deque(maxlen=20)
        self.pending_screen = None
        self.screen_preview_active = False
        self.preview_seen = deque(maxlen=256)
        for signal, slot in (
            (self.signals.mic, self._mic), (self.signals.partial, self._partial),
            (self.signals.level, self._level), (self.signals.phrase, self._phrase),
            (self.signals.voice_evidence, self._voice_evidence),
            (self.signals.owner_notice, self._owner_notice),
            (self.signals.input_quality, self._input_quality),
            (self.signals.safety_stop, self._safety_stop),
            (self.signals.action_event, self._action_event),
            (self.signals.error, self._capture_error), (self.signals.stage, self._stage),
            (self.signals.answer, self._answer), (self.signals.pcm, self._pcm),
            (self.signals.failed, self._failed),
            (self.signals.dialog_done, self._dialog_done),
        ):
            signal.connect(slot, Qt.ConnectionType.QueuedConnection)
        self.playback.started.connect(self._play_started)
        self.playback.finished.connect(self._play_finished)
        self.playback.error.connect(self._play_error)
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._poll)
        self.timer.start()

    def enable_microphone(self) -> None:
        if self.closing or self.capture is not None:
            return
        if self.reconfiguring:
            raise RuntimeError('Применяются настройки. Микрофон станет доступен после завершения.')
        if self.runtime is not None and self.runtime.shutdown_requested:
            raise RuntimeError('Службы применяют новые настройки. Микрофон станет доступен после завершения.')
        self.settings.validate()
        try:
            if self.runtime is None:
                self.runtime = DialogRuntime(self.settings, self.signals, self.metrics, warmup=True, conversation=self.conversation)
                self.runtime.start()
            self.epoch += 1
            self.capture = CaptureWorker(self.settings, self.signals, self.metrics, self.epoch)
            self.capture.playback_reference = self.playback_reference
            if self.power_pending or self.automation_active:
                self.capture.control_active.set()
            if self.guard.active is not None:
                self.capture.response_active.set()
            self.mic_state = 'loading'
            self.capture.start()
        except Exception as error:
            logging.getLogger(__name__).error('stage=worker_start_failed error_type=%s', type(error).__name__)
            self.disable_microphone()
            if self.capture is not None and not self.capture.is_alive():
                self.capture = None
            if self.runtime is not None:
                self.runtime.shutdown()
                if not self.runtime.is_alive():
                    self.runtime = None
            self.mic_state = 'error'
            self.notification.emit('Не удалось запустить фоновые службы. Попробуйте включить микрофон снова.')
        self.changed.emit()

    def disable_microphone(self):
        self.end_ptt()
        self.fair_gate.reset()
        self.resume_after_reconfigure = False
        self.cancel_response()
        if self.capture is not None:
            self.capture.stop()
        self.mic_state = 'stopping' if self.capture else 'off'
        self.changed.emit()

    def cancel_response(self, *, preserve_capture=False, preserve_refinement=None):
        if self.capture:
            cancel_refinement = getattr(self.capture, 'cancel_refinement', None)
            if cancel_refinement is not None:
                cancel_refinement(preserve_id=preserve_refinement)
        if self.echo.active_start is not None:
            self.fair_block_until=perf_counter()+self.echo.tail_seconds
        self.pending_text = None
        self.pending_screen = None
        self.screen_preview_active = False
        self.guard.cancel()
        self.pcm_queue.clear()
        self.current_chunk = None
        self.pcm_bytes = 0
        self.chunk_sequences.clear()
        self._first_text_recorded = False
        self._last_pcm_finished = None
        self.prefetched_sequences.clear()
        self.response_paused = False
        self.output_started = self.first_voice_recorded = self.producer_done = False
        if self.runtime:
            self.runtime.cancel()
        self.playback.stop()
        now = perf_counter()
        if not preserve_capture:
            self.accept_after = now
            self.voice_interruptions.clear()
        self.echo.end(now)
        self._end_busy(now)
        if self.capture and not preserve_capture:
            self.capture.reset_phrase()
        if self.capture:
            self.capture.response_active.clear()
        self.stage = 'idle'
        self.partial = ''
        self.changed.emit()

    def _action_event(self, event):
        event_turn = event.get('turn_id')
        current_turn = str(self.guard.active) if self.guard.active is not None else self.conversation.task.turn_id
        if event_turn and str(event_turn) != current_turn:
            return
        self.action_log.append({k: event[k] for k in ('status', 'tool', 'message', 'time') if k in event})
        self.action_log = self.action_log[-40:]
        self.automation_active = bool(event.get('active'))
        self.power_pending = bool(event.get('power_pending'))
        if self.capture:
            if self.power_pending or self.automation_active:
                self.capture.control_active.set()
            else:
                self.capture.control_active.clear()
        self.control_summary = event.get('summary') or (event.get('message', '') if event.get('active') else '')
        self.changed.emit()
        if event.get('ack') is not None:
            self.confirmation_visible.emit(event['ack'])

    def stop_actions(self):
        self.end_ptt()
        self.ptt_tokens.clear()
        self.fair_gate.reset()
        try:
            from .context_tools import ToolRouter
            router = getattr(self.runtime, 'router', None)
            if isinstance(router, ToolRouter):
                router.stop()
        finally:
            self.cancel_response()
            self.automation_active = self.power_pending = False
            if self.capture:
                self.capture.control_active.clear()
            self.control_summary = 'Действия остановлены.'
            self.changed.emit()

    def request_text(self, text):
        if self.closing or self.reconfiguring or not text.strip() or len(text) > 2000:
            return
        if self.guard.active is not None:
            self.cancel_response(preserve_capture=True)
        token = self.guard.begin(uuid4().hex)
        if token is None:
            return
        from .fair_mode import LocalReply
        self.local_notice=isinstance(text,LocalReply)
        self.recognized, self.partial, self.answer = ('' if self.local_notice else text.strip()), '', ''
        self.last_error = ''
        self.chunk_sequences.clear()
        self._first_text_recorded = False
        self._last_pcm_finished = None
        self.output_started = self.first_voice_recorded = self.producer_done = self.response_paused = False
        self.busy_start = self.speech_end = perf_counter()
        if self.capture:
            self.capture.response_active.set()
        self.stage = 'llm'
        try:
            if self.runtime is None:
                self.runtime = DialogRuntime(self.settings, self.signals, self.metrics, warmup=True, conversation=self.conversation)
                self.runtime.start()
            from .fair_mode import LocalReply
            queued = LocalReply(text.strip()) if isinstance(text,LocalReply) else self.recognized
            self.pending_text = (token, queued, perf_counter())
        except Exception:
            self._failed(token, 'Не удалось запустить обработку команды.')
        self.changed.emit()

    def toggle_pause(self):
        if self.guard.active is None:
            return
        self.response_paused = not self.response_paused
        if not self.response_paused:
            self.playback.resume()
            self._play_next()
            self._prefetch_pcm()
        else:
            self.playback.pause()
        self.changed.emit()

    def apply_settings(self, settings):
        if self.capture is not None or self.guard.active is not None:
            raise RuntimeError('Отключите микрофон перед изменением настроек.')
        settings.validate()
        if self.runtime is not None:
            self.runtime.shutdown()
        self.settings = settings
        self.changed.emit()

    def reconfigure(self, settings, *, credentials_changed=False) -> None:
        """Cancel once, drain old workers by polling, then resume prior mic intent."""
        settings.validate()
        if self.closing:
            raise RuntimeError('Приложение завершает работу.')
        changed = {key for key, value in settings.to_dict().items()
                   if value != getattr(self.settings, key)}
        if not credentials_changed and not self.reconfiguring and changed <= {'volume', 'reduced_motion'}:
            self.settings = settings
            self.playback.set_volume(settings.volume)
            self.settings_applied.emit(settings)
            self.changed.emit()
            return
        resume = self.resume_after_reconfigure if self.reconfiguring else (
            self.capture is not None and not self.capture.stop_event.is_set()
            and self.mic_state in ('listening', 'loading'))
        already_stopping = self.reconfiguring
        self.pending_settings = settings
        self.reconfiguring = True
        if not already_stopping:
            self.disable_microphone()
            if self.runtime is not None:
                self.runtime.shutdown()
        self.resume_after_reconfigure = resume
        self.changed.emit()

    def set_volume(self, value: float) -> None:
        self.settings = replace(self.settings, volume=value).validate()
        if self.pending_settings is not None:
            self.pending_settings = replace(self.pending_settings, volume=value)
        self.playback.set_volume(value)
        self.changed.emit()

    def shutdown(self):
        if self.closing:
            return
        self.closing = True
        self.disable_microphone()
        self.playback.close()
        if self.runtime:
            self.runtime.shutdown()

    def _poll(self):
        from .fair_mode import enabled
        now=perf_counter()
        if self.fair_gate.settings is not self.settings:
            from .fair_mode import ActivationGate
            self.fair_gate=ActivationGate(self.settings)
        if enabled(self.settings):
            blocked=self.echo.active_start is not None or now<self.fair_block_until
            if self.capture:
                self.capture.command_window_until=self.fair_gate.deadline
                if blocked and not self.settings.headphones:self.capture.playback_suppressed.set()
                else:self.capture.playback_suppressed.clear()
            state=('Останавливаю озвучку…' if self.ptt_down and blocked else 'Слушаю вас' if self.ptt_down or now<self.fair_gate.deadline else
                   'Команды во время озвучки выключены (без AEC)' if blocked and not self.settings.headphones else 'Ярмарка: ожидаю имя или удержание кнопки')
            if state!=self.fair_state:self.fair_state=state;self.changed.emit()
        self.ptt_tokens={k:v for k,v in self.ptt_tokens.items() if v>now}
        from .context_tools import ToolRouter
        router = getattr(self.runtime, 'router', None)
        if isinstance(router, ToolRouter):
            router.poll()
        if self.pending_text is not None:
            token, text, started = self.pending_text
            if self.runtime is None or perf_counter() - started > self.settings.timeout_s:
                self.pending_text = None
                self._failed(token, 'Службы не готовы. Повторите команду после паузы.')
            elif self.runtime.ready.is_set():
                self.pending_text = None
                if not self.runtime.submit(token, text):
                    self._failed(token, 'Не удалось передать команду. Повторите после паузы.')
        if self.capture is not None and not self.capture.is_alive():
            self.capture = None
            if self.mic_state != 'error':
                self.mic_state = 'off'
            self.changed.emit()
        if self.runtime is not None and not self.runtime.is_alive():
            self.runtime = None
            self.changed.emit()
        if self.pending_screen is not None:
            token, prompt, queued_at, image_jpeg = self.pending_screen
            if self.runtime is None or perf_counter() - queued_at > self.settings.timeout_s:
                self.pending_screen = None
                self._failed(token, 'Не удалось запустить анализ экрана. Попробуйте снова.')
            elif self.runtime.ready.is_set():
                self.pending_screen = None
                options = {'capture_screen': True} if image_jpeg is None else {'image_jpeg': image_jpeg}
                if not self.runtime.submit(token, prompt, **options):
                    self._failed(token, 'Службы диалога ещё не готовы. Повторите запрос.')
        if self.reconfiguring and self.capture is None and self.runtime is None:
            settings = self.pending_settings
            resume = self.resume_after_reconfigure and not self.closing
            self.pending_settings = None
            self.reconfiguring = self.resume_after_reconfigure = False
            if not self.closing:
                self.settings = settings
                task = self.conversation.task
                if (task.last_action.get('tool') == 'select_model' and task.last_action.get('status') == 'pending'
                        and task.last_action.get('arguments', {}).get('model') == settings.ollama_model):
                    from .context_tools import ToolResult
                    task.record('select_model', {'model': settings.ollama_model},
                                ToolResult('done', 'Настройки модели применены.', verified=True), task.turn_id)
                self.mic_state = 'off'
                self.settings_applied.emit(settings)
                if resume:
                    self.enable_microphone()
                self.changed.emit()
        if self.closing and self.capture is None and self.runtime is None:
            self.timer.stop()
            self.closed.emit()

    def _mic(self, epoch, state):
        if epoch != self.epoch:
            return
        if self.mic_state == 'error' and state == 'off':
            return
        if self.capture and self.capture.stop_event.is_set() and state in ('loading', 'listening'):
            return
        self.mic_state = state
        if state == 'error':
            self.cancel_response()
        self.changed.emit()

    def _partial(self, epoch, generation, text):
        if (epoch == self.epoch and self.capture is not None
                and generation == self.capture.generation
                and self.mic_state == 'listening' and self.guard.active is None):
            self.partial = text
            self.stage = 'recognition' if text else 'idle'
            self.changed.emit()

    def _level(self, epoch, value):
        if epoch == self.epoch:
            self.level = value
            self.changed.emit()

    def _capture_error(self, epoch, message):
        if epoch == self.epoch:
            self.notification.emit(message)

    def _valid_voice(self, epoch, phrase, *, explicit_hold=False):
        if self.capture and self.settings.stt_backend == 'hybrid' and not self.settings.fair_mode:
            latest = getattr(self.capture, 'refinement_utterance_id', None)
            if latest is not None and phrase['id'] != latest:
                return False
        if (epoch != self.epoch or self.mic_state != 'listening' or self.closing
                or (phrase['start'] < self.accept_after and not explicit_hold)):
            return False
        return self.capture is None or (
            phrase.get('generation') == self.capture.generation
            and ('refinement_generation' not in phrase or
                 phrase['refinement_generation'] == self.capture.refinement_generation)
            and phrase.get('revision') == self.capture.inbox.revision
            and not self.capture.inbox.damaged.is_set())

    def _user_evidence(self, phrase):
        overlap = self.echo.overlaps(phrase.get('audio_start', phrase['start']), phrase['ready'])
        if not overlap:
            return True
        # Headphones are an output preference, not proof that microphone
        # audio belongs to the user. Leakage and ASR hallucinations still occur.
        if phrase.get('acoustic') == 'echo' or matches_assistant(phrase['text'], self.answer):
            return False
        if self.settings.headphones:
            return True
        return phrase.get('acoustic') == 'independent'

    def _interrupt_voice(self, phrase):
        identity = phrase['id']
        if identity in self.voice_interruptions:
            return
        self.cancel_response(preserve_capture=True, preserve_refinement=identity)
        if len(self.voice_interruptions) >= 32:
            self.voice_interruptions.pop(next(iter(self.voice_interruptions)))
        self.voice_interruptions[identity] = (phrase.get('generation'), phrase.get('revision'))
        self.metrics.record('voice_start_to_output_reset', perf_counter() - phrase['start'])

    def _voice_evidence(self, epoch, phrase):
        if self.testing_microphone:return
        from .fair_mode import enabled
        if enabled(self.settings):return  # Partials never activate commands in a crowd.
        if (not self._valid_voice(epoch, phrase) or not phrase.get('stable')
                or not phrase['text'].strip() or phrase['id'] in self.consumed_controls
                or phrase['id'] in self.guard.seen or phrase['id'] in self.voice_interruptions):
            return
        if self.guard.active is not None and self._user_evidence(phrase):
            self._interrupt_voice(phrase)

    def _owner_notice(self, epoch):
        from .fair_mode import enabled
        if epoch == self.epoch and enabled(self.settings) and self.settings.owner_only and not self.closing:
            if not self.ptt_down:
                self.notification.emit('Для фоновой активации не удалось подтвердить владельца. Повторите или удерживайте кнопку.')

    def _input_quality(self,epoch,quality):
        if epoch!=self.epoch:return
        self.input_speech=bool(quality.get('speech'))
        if quality.get('clipping',0)>.01 and perf_counter()-self.last_fair_notice>5:
            self.last_fair_notice=perf_counter()
            self.notification.emit('Микрофон перегружен. Уменьшите усиление или отойдите немного дальше.')

    def begin_ptt(self):
        from .fair_mode import enabled
        if not enabled(self.settings) or self.ptt_down or self.closing or self.reconfiguring or self.testing_microphone:return
        self.cancel_response(preserve_capture=True)
        if self.capture is None:self.enable_microphone()
        if self.capture is None:return
        now=perf_counter();token=uuid4().hex
        self.ptt_down=True
        self.ptt_tokens[token]=now+self.settings.max_phrase_s+5
        floor=max(0,now-self.settings.preroll_ms/1000)
        if not self.settings.headphones:floor=max(floor,self.fair_block_until+.032)
        self.capture.hold.press(token,now,floor)
        self.changed.emit()

    def end_ptt(self):
        if not self.ptt_down:return
        self.ptt_down=False
        if self.capture:self.capture.hold.release(perf_counter())
        self.changed.emit()

    def _noisy(self):
        from .fair_mode import NOISY
        if perf_counter()-self.last_fair_notice<5:return
        self.last_fair_notice=perf_counter()
        self.notification.emit(NOISY)

    def _safety_stop(self, epoch, word='стоп'):
        # This path can only cancel; it never bypasses speaker verification for actions.
        if epoch != self.epoch or self.closing or self.testing_microphone:
            return
        if (self.power_pending and word in ('стоп', 'нет', 'отмена')) or (word == 'стоп' and self.automation_active):
            self.stop_actions()

    def _phrase(self, epoch, phrase):
        if self.testing_microphone:return
        from .fair_mode import enabled,words
        fair=enabled(self.settings)
        ptt=fair and phrase.get('ptt') is True and phrase.get('ptt_token') in self.ptt_tokens
        wake_only=fair and words(phrase['text'])==words(self.settings.wake_name) and self.settings.wake_enabled
        if fair and self.settings.owner_only and phrase.get('owner_verified') is not True and not (ptt or wake_only):
            return
        if not self._valid_voice(epoch, phrase,explicit_hold=ptt) or phrase['id'] in self.consumed_controls:
            return
        raw_query = phrase['text']
        if not fair and not raw_query.strip():
            self._noisy()
            return
        from .fair_mode import protocol_command
        protocol=protocol_command(raw_query,self.settings.wake_name)
        if protocol is not None:
            if (self.echo.overlaps(phrase.get('audio_start',phrase['start']),phrase['ready'])
                    or phrase.get('confidence') is None or phrase.get('confidence',1.)<self.settings.fair_stt_confidence):
                return
            self.consumed_controls.append(phrase['id'])
            self.stop_actions()
            self.fair_protocol_requested.emit(protocol)
            return
        if fair:
            if self.fair_gate.settings is not self.settings:
                from .fair_mode import ActivationGate
                self.fair_gate=ActivationGate(self.settings)
            if not self.settings.headphones and self.echo.overlaps(phrase.get('audio_start',phrase['start']),phrase['ready']):return
            if ptt:
                self.ptt_tokens.pop(phrase['ptt_token'],None)
                activated=raw_query
            else:
                if not raw_query.strip() and self.fair_gate.opened<=phrase['start']<=self.fair_gate.deadline and self.fair_gate.deadline:
                    self.fair_gate.reset();self._noisy();return
                activated=self.fair_gate.accept(raw_query,phrase['start'],phrase['ready'])
            if activated is None:return
            if not raw_query.strip() or phrase.get('confidence') is None or phrase.get('confidence',1.)<self.settings.fair_stt_confidence:
                self.fair_gate.reset();self._noisy();return
            if activated=='':self.changed.emit();return
            raw_query=activated
            self.fair_gate.reset()
        if raw_query.strip().casefold().strip(' .!?') in ('нет', 'отмена') and self.power_pending:
            self.stop_actions()
            return
        # Only direct user speech advances a power dialog. Reject all audio
        # overlapping Alice's prompt, even in user-selected headphone mode.
        if self.power_pending and self.echo.overlaps(phrase.get('audio_start', phrase['start']), phrase['ready']):
            return
        from .assistant_actions import parse_action
        request = parse_action(raw_query)
        # Only an explicit fact-deletion command conflicts with the word
        # "забудь" used by self-correction. All other requests retain cleanup.
        clean_query = (raw_query.strip() if request is not None and request[0] == 'forget'
                       else self.disfluency_filter.clean(raw_query))
        logging.getLogger(__name__).info('stage=stt_query raw_chars=%d clean_chars=%d',
                                         len(raw_query), len(clean_query))
        interrupted = self.voice_interruptions.get(phrase['id']) == (
            phrase.get('generation'), phrase.get('revision')) and phrase['id'] in self.voice_interruptions
        user = interrupted or self._user_evidence(phrase)
        if is_response_stop(raw_query):
            if user:
                if self.power_pending or self.automation_active:
                    self.stop_actions()
                if self.guard.active is not None:
                    self._interrupt_voice(phrase)
                elif self.capture:
                    self.capture.cancel_refinement()
                self.voice_interruptions.pop(phrase['id'], None)
                self.consumed_controls.append(phrase['id'])
            return
        # Control commands use the original utterance; cleanup cannot create one.
        if is_stop_command(raw_query.strip()):
            if user:
                self.disable_microphone()
            return
        self.partial = ''
        if clean_query and user and self.guard.active is not None and phrase['id'] not in self.guard.seen:
            self._interrupt_voice(phrase)
            interrupted = True
        if (not clean_query or not user or self.screen_preview_active
                or (not ptt and not interrupted and self._busy_overlap(phrase['start'], phrase['ready']))):
            if self.guard.active is None:
                self.stage = 'idle'
            self.changed.emit()
            return
        self.voice_interruptions.pop(phrase['id'], None)
        if is_screen_request(clean_query):
            self.request_screen_preview(clean_query, phrase['id'])
            return
        self.last_error = ''
        token = self.guard.begin(phrase['id'])
        if token is None:
            return
        self.recognized = clean_query
        self.local_notice=False
        if self.capture:
            self.capture.response_active.set()
        self.answer = ''
        self.chunk_sequences.clear()
        self._first_text_recorded = False
        self._last_pcm_finished = None
        self.output_started = self.first_voice_recorded = self.producer_done = False
        self.response_paused = False
        self.speech_end = phrase['speech_end']
        self.busy_start = perf_counter()
        if self.runtime is None or not self.runtime.submit(token, clean_query):
            self._failed(token, 'Службы диалога ещё не готовы. Повторите фразу после паузы.')
        self.changed.emit()

    def request_screen_preview(self, prompt: str = 'Посмотри на экран и опиши, что на нём.',
                               utterance_id: str = '') -> None:
        if self.closing or self.reconfiguring or self.guard.active is not None or self.screen_preview_active:
            return
        if not self.settings.permission_screen:
            self.notification.emit('Анализ экрана выключен. Включите его в настройках.')
            return
        if self.settings.provider != 'gemini':
            self.notification.emit('Для анализа экрана выберите Gemini с поддержкой изображений.')
            return
        if utterance_id and (utterance_id in self.preview_seen or utterance_id in self.guard.seen):
            return
        if utterance_id:
            self.preview_seen.append(utterance_id)
        self.screen_preview_active = True
        self.screen_preview_requested.emit(prompt, utterance_id)
        self.changed.emit()

    def request_screen(self, prompt: str = 'Посмотри на экран и опиши, что на нём.',
                       *, utterance_id: str | None = None, image_jpeg: bytes | None = None) -> None:
        """Queue one explicit vision turn without opening the microphone."""
        if self.closing or self.reconfiguring or self.guard.active is not None:
            return
        if not self.settings.permission_screen:
            self.notification.emit('Анализ экрана выключен. Включите его в настройках.')
            return
        if self.settings.provider != 'gemini':
            self.notification.emit('Для анализа экрана выберите Gemini с поддержкой изображений.')
            return
        if not prompt.strip():
            self.notification.emit('Введите вопрос к снимку экрана.')
            return
        if image_jpeg is not None and (not isinstance(image_jpeg, bytes)
                or not image_jpeg.startswith(b'\xff\xd8\xff') or len(image_jpeg) > MAX_IMAGE_BYTES):
            self.notification.emit('Неверный или слишком большой снимок экрана.')
            return
        if self.runtime is not None and self.runtime.shutdown_requested:
            self.notification.emit('Службы диалога перезапускаются. Повторите запрос после паузы.')
            return
        self.last_error = ''
        token = self.guard.begin(utterance_id or uuid4().hex)
        if token is None:
            return
        self.recognized, self.partial, self.answer = prompt, '', ''
        if self.capture:
            self.capture.response_active.set()
        self.stage = 'vision'
        self.busy_start = self.speech_end = perf_counter()
        self.chunk_sequences.clear()
        self._first_text_recorded = False
        self._last_pcm_finished = None
        self.output_started = self.first_voice_recorded = self.producer_done = False
        self.response_paused = False
        try:
            if self.runtime is None:
                self.runtime = DialogRuntime(self.settings, self.signals, self.metrics, warmup=True, conversation=self.conversation)
                self.runtime.start()
            self.pending_screen = (token, prompt, perf_counter(), image_jpeg)
            self._poll()
        except Exception:
            self.pending_screen = None
            self._failed(token, 'Не удалось запустить анализ экрана. Попробуйте снова.')
        self.changed.emit()

    def _busy_overlap(self, start, end):
        return any(start <= right and end >= left for left, right in self.busy_intervals)

    def _end_busy(self, now):
        if self.busy_start:
            self.busy_intervals.append((self.busy_start, now))
            self.busy_intervals = self.busy_intervals[-64:]
            self.busy_start = 0

    def _stage(self, token, stage):
        if self.guard.valid(token):
            if not self.output_started:
                self.stage = stage
            self.changed.emit()

    def _answer(self, token, answer):
        if self.guard.valid(token):
            if answer.strip() and not self._first_text_recorded:
                self._first_text_recorded = True
                self.metrics.record('speech_end_to_first_text', perf_counter() - self.speech_end)
            self.answer = answer
            self.changed.emit()

    def _model_selected(self, token, model):
        if self.guard.valid(token) and not self.closing and not self.reconfiguring:
            self.model_change_requested.emit(model)

    def _pcm(self, token, pcm, ready, sequence=0):
        if not self.guard.valid(token) or self.closing:
            return
        if self.capture:
            self.capture.response_active.set()
        if sequence in self.chunk_sequences:
            return
        if self.pcm_bytes + len(pcm.data) > MAX_PCM_BYTES:
            self._failed(token, 'Очередь воспроизведения превысила ограничение PCM-буфера.')
            return
        self.chunk_sequences.add(sequence)
        self.pcm_bytes += len(pcm.data)
        self.pcm_queue.append((sequence, pcm, ready))
        self._play_next()
        self._prefetch_pcm()
        self.changed.emit()

    def _prefetch_pcm(self):
        if self.current_chunk is None or self.response_paused:
            return
        for sequence, pcm, _ in self.pcm_queue:
            if sequence in self.prefetched_sequences:
                continue
            try:
                if not self.playback.append(pcm):
                    break
                self.prefetched_sequences.add(sequence)
            except RuntimeError as error:
                self._failed(self.guard.active, str(error))
                break

    def _play_next(self):
        if self.current_chunk is not None or not self.pcm_queue or self.response_paused:
            return
        sequence, pcm, ready = self.pcm_queue.popleft()
        self.current_chunk = (sequence, pcm, ready)
        self.pcm_ready = ready
        self.stage = 'playback'
        if not self.output_started:
            from .fair_mode import enabled
            if self.capture and enabled(self.settings) and not self.settings.headphones:
                self.capture.playback_suppressed.set()
            self.echo.start(perf_counter())
            self.output_started = True
        try:
            if sequence in self.prefetched_sequences:
                self.prefetched_sequences.remove(sequence)
            else:
                self.playback.play(pcm, self.settings.volume, self.settings.output_device)
        except RuntimeError as error:
            self._failed(self.guard.active, str(error))

    def _play_started(self, when):
        if self.current_chunk is None:
            return
        self.playback_reference.add(self.current_chunk[1], when)
        self.metrics.record('pcm_to_playback', when - self.pcm_ready)
        if self._last_pcm_finished is not None:
            self.metrics.record('playback_phrase_gap', when - self._last_pcm_finished)
            self._last_pcm_finished = None
        if not self.first_voice_recorded:
            self.first_voice_recorded = True
            self.metrics.record('speech_end_to_voice', when - self.speech_end)
            logging.getLogger(__name__).info('stage=first_playback duration_ms=%.1f',
                                             (when - self.speech_end) * 1000)

    def _play_finished(self):
        if self.current_chunk is None:
            return
        sequence, pcm, _ = self.current_chunk
        self._last_pcm_finished = perf_counter()
        self.current_chunk = None
        self.pcm_bytes -= len(pcm.data)
        if self.runtime is not None and self.guard.active is not None:
            self.runtime.release_pcm(self.guard.active, sequence)
        self._play_next()
        self._finish_if_done()
        self._prefetch_pcm()
        self.changed.emit()

    def _dialog_done(self, token: int) -> None:
        if not self.guard.valid(token):
            return
        self.producer_done = True
        self._finish_if_done()

    def _finish_if_done(self) -> None:
        if not self.producer_done or self.current_chunk is not None or self.pcm_queue:
            return
        now = perf_counter()
        self.fair_block_until=now+self.echo.tail_seconds
        self.echo.end(now)
        if self.settings.fair_continue:
            self.fair_gate.opened=now;self.fair_gate.deadline=now+self.settings.wake_window_s
        self._end_busy(now)
        self.guard.finish(self.guard.active)
        if self.capture:
            self.capture.response_active.clear()
        if not self.local_notice:
            self.completed_turns.append((self.recognized, self.answer))
            self.turn_finished.emit(self.recognized, self.answer)
        self.stage = 'idle'
        self.response_paused = False
        self.changed.emit()

    def _play_error(self, message):
        self._failed(self.guard.active, message)

    def _failed(self, token: int | None, message: str) -> None:
        if token == -1:
            self.disable_microphone()
            self.mic_state = 'error'
            self.changed.emit()
            self.notification.emit(message)
            return
        if self.guard.valid(token):
            self.cancel_response()
            self.last_error = message
            self.notification.emit(message)
            self.changed.emit()
