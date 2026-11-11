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
    changed = pyqtSignal()
    notification = pyqtSignal(str)
    closed = pyqtSignal()
    settings_applied = pyqtSignal(object)
    turn_finished = pyqtSignal(str, str)
    screen_preview_requested = pyqtSignal(str, str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.disfluency_filter = DisfluencyFilter()
        self.metrics = LatencyMetrics()
        self.signals = WorkerSignals(self)
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
                self.runtime = DialogRuntime(self.settings, self.signals, self.metrics, warmup=True)
                self.runtime.start()
            self.epoch += 1
            self.capture = CaptureWorker(self.settings, self.signals, self.metrics, self.epoch)
            self.capture.playback_reference = self.playback_reference
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
        self.resume_after_reconfigure = False
        self.cancel_response()
        if self.capture is not None:
            self.capture.stop()
        self.mic_state = 'stopping' if self.capture else 'off'
        self.changed.emit()

    def cancel_response(self, *, preserve_capture=False):
        self.pending_screen = None
        self.screen_preview_active = False
        self.guard.cancel()
        self.pcm_queue.clear()
        self.current_chunk = None
        self.pcm_bytes = 0
        self.chunk_sequences.clear()
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

    def _valid_voice(self, epoch, phrase):
        if (epoch != self.epoch or self.mic_state != 'listening' or self.closing
                or phrase['start'] < self.accept_after):
            return False
        return self.capture is None or (
            phrase.get('generation') == self.capture.generation
            and phrase.get('revision') == self.capture.inbox.revision
            and not self.capture.inbox.damaged.is_set())

    def _user_evidence(self, phrase):
        overlap = self.echo.overlaps(phrase.get('audio_start', phrase['start']), phrase['ready'])
        if not overlap:
            return True
        if self.settings.headphones:
            return True  # User-selected isolated output; STT confirmation still required.
        return (phrase.get('acoustic') == 'independent'
                and not matches_assistant(phrase['text'], self.answer))

    def _interrupt_voice(self, phrase):
        identity = phrase['id']
        if identity in self.voice_interruptions:
            return
        self.cancel_response(preserve_capture=True)
        if len(self.voice_interruptions) >= 32:
            self.voice_interruptions.pop(next(iter(self.voice_interruptions)))
        self.voice_interruptions[identity] = (phrase.get('generation'), phrase.get('revision'))
        self.metrics.record('voice_start_to_output_reset', perf_counter() - phrase['start'])

    def _voice_evidence(self, epoch, phrase):
        if (not self._valid_voice(epoch, phrase) or not phrase.get('stable')
                or not phrase['text'].strip() or phrase['id'] in self.consumed_controls
                or phrase['id'] in self.guard.seen or phrase['id'] in self.voice_interruptions):
            return
        if self.guard.active is not None and self._user_evidence(phrase):
            self._interrupt_voice(phrase)

    def _phrase(self, epoch, phrase):
        if not self._valid_voice(epoch, phrase) or phrase['id'] in self.consumed_controls:
            return
        raw_query = phrase['text']
        clean_query = self.disfluency_filter.clean(raw_query)
        logging.getLogger(__name__).info('stage=stt_query raw_query=%r clean_query=%r',
                                         raw_query, clean_query)
        interrupted = self.voice_interruptions.get(phrase['id']) == (
            phrase.get('generation'), phrase.get('revision')) and phrase['id'] in self.voice_interruptions
        user = interrupted or self._user_evidence(phrase)
        if is_response_stop(raw_query):
            if user:
                if self.guard.active is not None:
                    self._interrupt_voice(phrase)
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
                or (not interrupted and self._busy_overlap(phrase['start'], phrase['ready']))):
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
        if self.capture:
            self.capture.response_active.set()
        self.answer = ''
        self.chunk_sequences.clear()
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
        self.output_started = self.first_voice_recorded = self.producer_done = False
        self.response_paused = False
        try:
            if self.runtime is None:
                self.runtime = DialogRuntime(self.settings, self.signals, self.metrics, warmup=True)
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
            self.answer = answer
            self.changed.emit()

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
        if not self.first_voice_recorded:
            self.first_voice_recorded = True
            self.metrics.record('speech_end_to_voice', when - self.speech_end)
            logging.getLogger(__name__).info('stage=first_playback duration_ms=%.1f',
                                             (when - self.speech_end) * 1000)

    def _play_finished(self):
        if self.current_chunk is None:
            return
        sequence, pcm, _ = self.current_chunk
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
        self.echo.end(now)
        self._end_busy(now)
        self.guard.finish(self.guard.active)
        if self.capture:
            self.capture.response_active.clear()
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
