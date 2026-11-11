import asyncio
import logging
from collections import deque
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from queue import Empty
from threading import Event, Thread, Lock
from time import perf_counter

from PyQt6.QtCore import QObject, pyqtSignal

from .audio import AudioCapture, FrameInbox, RATE, pcm_rms, list_input_devices
from .recognition import PhraseSegmenter, VoskRecognizer
from .vad import SileroVAD
from .providers import LLMProvider, ElevenLabsTTSProvider
from .speech import SAPISynthesizer, EdgeSynthesizer, clean_speech, MAX_PCM_BYTES
from .silero_tts import SileroSynthesizer
from .streaming import SentenceChunker
from .utils.vision import capture_screen_jpeg

LOGGER = logging.getLogger(__name__)


class WorkerSignals(QObject):
    mic = pyqtSignal(int, str)
    partial = pyqtSignal(int, int, str)
    phrase = pyqtSignal(int, object)
    voice_evidence = pyqtSignal(int, object)
    level = pyqtSignal(int, float)
    error = pyqtSignal(int, str)
    stage = pyqtSignal(int, str)
    answer = pyqtSignal(int, str)
    pcm = pyqtSignal(int, object, float, int)
    failed = pyqtSignal(int, str)
    dialog_done = pyqtSignal(int)


class DeviceDiscoverySignals(QObject):
    devices = pyqtSignal(object)
    failed = pyqtSignal(str)


class MicrophoneDiscovery(Thread):
    """Read-only device lookup; dialog closing never waits for PortAudio."""
    def __init__(self, signals: DeviceDiscoverySignals):
        super().__init__(name='alice-devices', daemon=True)
        self.signals = signals

    def run(self) -> None:
        try:
            devices = list_input_devices()
        except Exception:
            try:
                self.signals.failed.emit('Не удалось получить микрофоны. Нажмите «Обновить».')
            except RuntimeError:
                pass  # The dialog was already destroyed.
        else:
            try:
                self.signals.devices.emit(devices)
            except RuntimeError:
                pass


class CaptureWorker(Thread):
    def __init__(self, settings, signals, metrics, epoch, capture_factory=AudioCapture,
                 vad_factory=SileroVAD, stt_factory=VoskRecognizer):
        super().__init__(name='alice-capture', daemon=False)
        self.settings, self.signals, self.metrics, self.epoch = settings, signals, metrics, epoch
        self.stop_event, self.reset_event = Event(), Event()
        self.response_active = Event()
        self.inbox = FrameInbox()
        self.generation_lock = Lock()
        self.generation = 0
        self.playback_reference = None
        self.capture_factory, self.vad_factory, self.stt_factory = capture_factory, vad_factory, stt_factory

    def stop(self):
        self.stop_event.set()

    def reset_phrase(self):
        with self.generation_lock:
            self.generation += 1
            self.reset_event.set()

    def run(self):
        capture = None
        try:
            self.signals.mic.emit(self.epoch, 'loading')
            started = perf_counter()
            vad = self.vad_factory(self.settings.vad_model)
            self.metrics.record('cold_vad', perf_counter() - started)
            LOGGER.info('stage=vad_loaded duration_ms=%.1f', (perf_counter() - started) * 1000)
            started = perf_counter()
            stt = self.stt_factory(self.settings.vosk_model)
            self.metrics.record('cold_stt', perf_counter() - started)
            LOGGER.info('stage=stt_loaded duration_ms=%.1f', (perf_counter() - started) * 1000)
            seg = PhraseSegmenter(self.settings.threshold, self.settings.silence_ms,
                                  self.settings.preroll_ms, self.settings.max_phrase_s,
                                  self.settings.min_speech_ms)
            if self.stop_event.is_set():
                return
            capture = self.capture_factory(self.inbox, self.settings.input_device)
            capture.open()
            self.signals.mic.emit(self.epoch, 'listening')
            quarantine = 0
            awaiting_silence = False
            next_level = next_partial = next_diagnostic = 0
            voice_frames = deque(maxlen=16)
            evidence_pcm = b''
            candidate_word, candidate_since, last_voice_end = '', 0., 0.
            while not self.stop_event.is_set():
                if self.inbox.device_error.is_set():
                    raise RuntimeError('Аудиоустройство остановилось. Проверьте подключение микрофона.')
                if self.reset_event.is_set() or self.inbox.damaged.is_set():
                    overflow = self.inbox.damaged.is_set()
                    self.reset_event.clear()
                    self.inbox.clear()
                    vad.reset(); stt.reset(); seg.reset()
                    voice_frames.clear()
                    evidence_pcm = b''
                    candidate_word, candidate_since = '', 0.
                    quarantine = 0
                    awaiting_silence = True
                    self.signals.partial.emit(self.epoch, self.generation, '')
                    if overflow:
                        self.signals.error.emit(self.epoch, 'Потеря аудиокадров: текущая фраза отброшена. Сделайте паузу и повторите.')
                try:
                    frame = self.inbox.get(timeout=.05)
                except Empty:
                    continue
                generation = self.generation
                revision = self.inbox.revision
                self.metrics.record('audio_queue', perf_counter() - frame.captured_at)
                started = perf_counter()
                probability = vad(frame.pcm, RATE)
                self.metrics.record('vad', perf_counter() - started)
                if frame.captured_at >= next_level:
                    rms = pcm_rms(frame.pcm)
                    self.signals.level.emit(self.epoch, rms)
                    next_level = frame.captured_at + .1
                if frame.captured_at >= next_diagnostic:
                    LOGGER.info('stage=audio_frame rms=%.6f vad=%.3f threshold=%.3f',
                                pcm_rms(frame.pcm), probability, self.settings.threshold)
                    next_diagnostic = frame.captured_at + .5
                if awaiting_silence:
                    quarantine = quarantine + 1 if probability < self.settings.threshold else 0
                    if quarantine >= seg.silence_frames:
                        awaiting_silence = False
                        vad.reset(); stt.reset(); seg.reset()
                    continue
                # Short commands such as "Стоп" can contain <320 ms of speech.
                # Only while responding, let STT examine >=128 ms candidates;
                # stable text/echo checks, never VAD alone, authorize barge-in.
                configured_min = (self.settings.min_speech_ms + 31) // 32
                seg.min_speech_frames = min(configured_min, 4) if self.response_active.is_set() else configured_min
                update = seg.feed(frame, probability)
                if update.started:
                    stt.reset()
                    voice_frames.clear()
                    evidence_pcm = b''
                    candidate_word, candidate_since = '', 0.
                    LOGGER.info('stage=speech_confirmed min_speech_ms=%d', self.settings.min_speech_ms)
                if update.discarded:
                    LOGGER.info('stage=speech_discarded reason=too_short')
                partial = ''
                for selected in update.frames:
                    partial = stt.feed(selected.pcm)
                    voice_frames.append(selected.pcm)
                if update.frames and probability >= self.settings.threshold:
                    # Snapshot a contiguous window at the last voiced frame;
                    # never compress away short VAD-negative gaps. Keep this
                    # snapshot while endpoint silence lets STT settle.
                    evidence_pcm = b''.join(voice_frames)
                    last_voice_end = frame.captured_at
                if update.frames and frame.captured_at >= next_partial:
                    self.signals.partial.emit(self.epoch, generation, partial)
                    next_partial = frame.captured_at + .1
                    first_word = partial.casefold().split()[:1]
                    first_word = first_word[0] if first_word else ''
                    if first_word != candidate_word:
                        candidate_word, candidate_since = first_word, frame.captured_at
                    stable = bool(first_word) and frame.captured_at - candidate_since >= .1
                    if (stable and generation == self.generation and revision == self.inbox.revision
                            and not self.inbox.damaged.is_set() and not self.reset_event.is_set()):
                        acoustic = (self.playback_reference.classify(evidence_pcm, last_voice_end)
                                    if self.playback_reference is not None else 'unknown')
                        self.signals.voice_evidence.emit(self.epoch, {
                            'id': update.utterance_id, 'text': partial, 'stable': True,
                            'start': update.phrase_start, 'audio_start': update.audio_start,
                            'speech_end': update.speech_end, 'ready': perf_counter(),
                            'generation': generation, 'revision': revision, 'acoustic': acoustic,
                        })
                if update.finished:
                    started = perf_counter()
                    text = stt.finalize()
                    ready = perf_counter()
                    if (self.inbox.damaged.is_set() or self.reset_event.is_set()
                            or self.stop_event.is_set() or generation != self.generation
                            or revision != self.inbox.revision):
                        continue
                    self.metrics.record('stt_finalize', ready - started)
                    self.metrics.record('endpoint_wait', frame.captured_at - update.speech_end)
                    self.metrics.record('speech_end_to_text', ready - update.speech_end)
                    self.signals.phrase.emit(self.epoch, {
                        'id': update.utterance_id, 'text': text,
                        'start': update.phrase_start, 'speech_end': update.speech_end,
                        'ready': ready, 'audio_start': update.audio_start,
                        'generation': generation, 'revision': revision,
                        'acoustic': (self.playback_reference.classify(evidence_pcm, last_voice_end)
                                     if self.playback_reference is not None else 'unknown'),
                    })
                    stt.reset()
                    # Maximum length can end mid-speech: don't launch the remainder.
                    if probability >= self.settings.threshold:
                        awaiting_silence = True
                        quarantine = 0
        except Exception as error:
            # Adapter RuntimeErrors are fixed safe strings; never show native exception text.
            message = str(error) if isinstance(error, RuntimeError) else 'Ошибка обработки аудио. Проверьте модели и устройство.'
            self.signals.error.emit(self.epoch, message)
            self.signals.mic.emit(self.epoch, 'error')
        finally:
            if capture is not None:
                try:
                    capture.close()
                except Exception:
                    self.signals.error.emit(self.epoch, 'Ошибка освобождения аудиоустройства.')
            self.inbox.clear()
            self.signals.mic.emit(self.epoch, 'off')


@dataclass
class StreamSession:
    token: int
    credits: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(3))
    played: asyncio.Event = field(default_factory=asyncio.Event)
    inflight: dict[int, int] = field(default_factory=dict)
    pcm_bytes: int = 0


class DialogRuntime(Thread):
    """One event loop and one synthesis executor, no GUI waits."""
    def __init__(self, settings, signals, metrics, *, warmup=False):
        super().__init__(name='alice-dialog', daemon=False)
        settings.validate()
        self.settings, self.signals, self.metrics = settings, signals, metrics
        self.ready = Event()
        self.lock = Lock()
        self.loop = None
        self.current = None
        self.unfinished_turns = set()
        self.cancel_generation = 0
        self.cancel_event = Event()
        self.shutdown_requested = False
        self.history = deque(maxlen=settings.history_turns * 2)
        self.stream_session: StreamSession | None = None
        self.warmup_enabled = warmup

    def run(self) -> None:
        loop = executor = None
        provider = None
        tts_provider = None
        started = perf_counter()
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='alice-speech')
            self.executor = executor
            provider = LLMProvider(self.settings)
            self.provider = provider
            if self.settings.synthesizer == 'elevenlabs':
                tts_provider = ElevenLabsTTSProvider(self.settings, executor)
                self.synth = tts_provider
            elif self.settings.synthesizer == 'silero':
                self.synth = SileroSynthesizer(self.settings, executor)
            else:
                kind = SAPISynthesizer if self.settings.synthesizer == 'sapi' else EdgeSynthesizer
                voice = self.settings.sapi_voice if kind is SAPISynthesizer else self.settings.edge_voice
                self.synth = kind(voice, executor, self.settings.timeout_s)
            with self.lock:
                self.loop = loop
                self.ready.set()
                shutting_down = self.shutdown_requested
            LOGGER.info('stage=dialog_ready duration_ms=%.1f', (perf_counter() - started) * 1000)
            if not shutting_down:
                if self.warmup_enabled:
                    loop.create_task(self._warmup())
                loop.run_forever()
        except Exception as error:
            # Never log exception text: native/network errors may include secrets.
            LOGGER.error('stage=dialog_runtime_failed error_type=%s', type(error).__name__)
            self.signals.failed.emit(-1, 'Не удалось запустить фоновые службы диалога.')
        finally:
            with self.lock:
                self.ready.clear()
                self.loop = None
                self.cancel_event.set()
            # Each cleanup is independent: one failure must not skip other resources.
            if loop is not None:
                self._cleanup('tasks', lambda: self._cancel_pending(loop))
                if provider is not None:
                    self._cleanup('provider', lambda: loop.run_until_complete(
                        asyncio.wait_for(provider.close(), timeout=5)))
                if tts_provider is not None:
                    self._cleanup('tts_provider', lambda: loop.run_until_complete(
                        asyncio.wait_for(tts_provider.close(), timeout=5)))
            if executor is not None:
                # Only the dialog worker waits; the GUI keeps processing events.
                self._cleanup('executor', lambda: executor.shutdown(wait=True, cancel_futures=True))
            if loop is not None:
                self._cleanup('asyncgens', lambda: loop.run_until_complete(loop.shutdown_asyncgens()))
                self._cleanup('loop', loop.close)
            self._cleanup('thread_loop', lambda: asyncio.set_event_loop(None))
            LOGGER.info('stage=dialog_stopped')

    async def _warmup(self):
        started = perf_counter()
        try:
            check = getattr(self.synth, 'check_configuration', None)
            if check is not None:
                check()  # Missing ElevenLabs key must not initiate LLM work.
            async with asyncio.timeout(min(5, self.settings.timeout_s)):
                await self.provider.warmup()
            self.metrics.record('provider_warmup', perf_counter() - started)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.info('stage=provider_warmup_unavailable error_type=%s', type(error).__name__)

    def _cleanup(self, stage: str, action: Callable[[], object]) -> None:
        try:
            action()
        except (Exception, asyncio.CancelledError) as error:
            LOGGER.error('stage=dialog_cleanup_%s error_type=%s', stage, type(error).__name__)
            self.signals.failed.emit(-1, 'Ошибка освобождения фоновых ресурсов диалога.')

    @staticmethod
    def _cancel_pending(loop: asyncio.AbstractEventLoop) -> None:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

    def _schedule(self, callback: Callable, *args: object) -> bool:
        """Serialize loop publication/teardown with cross-thread scheduling."""
        with self.lock:
            if not self.ready.is_set() or self.loop is None:
                return False
            try:
                self.loop.call_soon_threadsafe(callback, *args)
            except RuntimeError:
                return False  # The loop was already closed by the environment.
        return True

    def submit(self, token: int, text: str, *, capture_screen: bool = False,
               image_jpeg: bytes | None = None) -> bool:
        if not self.is_alive() or self.shutdown_requested:
            return False
        with self.lock:
            generation = self.cancel_generation
        return self._schedule(self._start_submitted, generation, token, text, capture_screen, image_jpeg)

    def _start_submitted(self, generation, token, text, capture_screen, image_jpeg):
        with self.lock:
            if generation == self.cancel_generation:
                self._start_locked(token, text, capture_screen, image_jpeg)

    def _start(self, token: int, text: str, capture_screen: bool = False,
               image_jpeg: bytes | None = None) -> None:
        with self.lock:
            self._start_locked(token, text, capture_screen, image_jpeg)

    def _start_locked(self, token, text, capture_screen, image_jpeg):
        if self.shutdown_requested:
            return
        previous = self.current
        if previous is not None and not previous.done() and not self.cancel_event.is_set():
            self.signals.failed.emit(token, 'Предыдущая обработка ещё отменяется. Повторите фразу после паузы.')
            return
        cancel = Event()
        self.cancel_event = cancel
        predecessors = tuple(task for task in self.unfinished_turns if not task.done())
        coroutine = self._after_cancel(predecessors, token, text, cancel, capture_screen, image_jpeg)
        try:
            self.current = self.loop.create_task(coroutine)
            self.unfinished_turns.add(self.current)
            self.current.add_done_callback(self.unfinished_turns.discard)
        except Exception as error:
            coroutine.close()
            self.cancel_event.set()
            LOGGER.error('stage=dialog_task_failed error_type=%s', type(error).__name__)
            self.signals.failed.emit(token, 'Не удалось запустить обработку ответа. Повторите запрос.')

    async def _after_cancel(self, predecessors, token, text, cancel, capture_screen, image_jpeg):
        if predecessors:
            # Serialize teardown/history/session ownership before the next turn.
            # Shield prevents cancellation of this replacement from re-cancelling
            # a provider already running its asynchronous cleanup.
            await asyncio.shield(asyncio.gather(*predecessors, return_exceptions=True))
        if not cancel.is_set():
            await self._process(token, text, cancel, capture_screen, image_jpeg)

    async def _process(self, token: int, text: str, cancel: Event,
                       capture_screen: bool = False, image_jpeg: bytes | None = None) -> None:
        session = StreamSession(token)
        session.played.set()
        self.stream_session = session
        sentences: asyncio.Queue[str | None] = asyncio.Queue(maxsize=2)
        answer_parts: list[str] = []
        spoken: list[str] = []
        started = perf_counter()

        async def produce() -> None:
            chunker = SentenceChunker(early_fragments=getattr(self.synth, 'accepts_fragments', False))
            next_update = 0.0
            LOGGER.info('stage=llm_start token=%d', token)
            async with aclosing(self.provider.stream(
                    list(self.history) + [{'role': 'user', 'content': text}],
                    lambda: self.metrics.record('llm_first', perf_counter() - started),
                    **({'image_jpeg': image_jpeg} if image_jpeg is not None else {}))) as stream:
                async for delta in stream:
                    if cancel.is_set():
                        raise asyncio.CancelledError
                    answer_parts.append(delta)
                    now = perf_counter()
                    if now >= next_update:
                        self.signals.answer.emit(token, clean_speech(''.join(answer_parts)))
                        next_update = now + .05  # Don't flood the GUI with per-token updates.
                    for sentence in chunker.feed(delta):
                        await sentences.put(sentence)
            self.metrics.record('llm_full', perf_counter() - started)
            self.signals.answer.emit(token, clean_speech(''.join(answer_parts)))
            for sentence in chunker.finish():
                await sentences.put(sentence)
            await sentences.put(None)

        async def synthesize() -> None:
            sequence = 0
            try:
                while (sentence := await sentences.get()) is not None:
                    sentence = clean_speech(sentence)
                    if not any(character.isalnum() for character in sentence):
                        continue
                    if cancel.is_set():
                        raise asyncio.CancelledError
                    if sequence == 0:
                        self.metrics.record('llm_first_sentence', perf_counter() - started)
                    self.signals.stage.emit(token, 'tts')
                    LOGGER.info('stage=tts_start token=%d chunk=%d', token, sequence)
                    tts_started = perf_counter()
                    async def pcm_chunks():
                        if hasattr(self.synth, 'stream_pcm'):
                            async with aclosing(self.synth.stream_pcm(sentence, cancel)) as stream:
                                async for packet in stream:
                                    yield packet
                        else:
                            yield await self.synth.synthesize(sentence, cancel)
                    received = False
                    async with aclosing(pcm_chunks()) as stream:
                        while True:
                            await session.credits.acquire()
                            try:
                                pcm = await anext(stream)
                            except StopAsyncIteration:
                                session.credits.release()
                                break
                            if cancel.is_set():
                                raise asyncio.CancelledError
                            if session.pcm_bytes + len(pcm.data) > MAX_PCM_BYTES:
                                raise RuntimeError('Очередь озвучки превысила ограничение PCM-буфера.')
                            session.inflight[sequence] = len(pcm.data)
                            session.pcm_bytes += len(pcm.data)
                            session.played.clear()
                            received = True
                            if sequence == 0:
                                self.metrics.record('tts_first_pcm', perf_counter() - tts_started)
                                self.metrics.record('llm_to_first_pcm', perf_counter() - started)
                            self.signals.pcm.emit(token, pcm, perf_counter(), sequence)
                            sequence += 1
                    self.metrics.record('tts', perf_counter() - tts_started)
                    if received:
                        spoken.append(sentence)
            except BaseException:
                cancel.set()  # Also stop an executor/COM job whose await was cancelled.
                raise

        try:
            if cancel.is_set():
                raise asyncio.CancelledError
            check = getattr(getattr(self, 'synth', None), 'check_configuration', None)
            if check is not None:
                check()  # Fail missing credentials before spending a LLM request.
            if isinstance(getattr(self, 'synth', None), SileroSynthesizer):
                await self.synth.prepare(cancel, lambda stage: self.signals.stage.emit(token, stage))
                if cancel.is_set():
                    raise asyncio.CancelledError
            if (capture_screen or image_jpeg is not None) and self.settings.provider != 'gemini':
                raise RuntimeError('Для анализа экрана выберите Gemini с поддержкой изображений.')
            if capture_screen:
                self.signals.stage.emit(token, 'vision')
                image_jpeg = await asyncio.get_running_loop().run_in_executor(
                    getattr(self, 'executor', None), capture_screen_jpeg)
                if cancel.is_set():
                    raise asyncio.CancelledError
            self.signals.stage.emit(token, 'llm')
            async with asyncio.TaskGroup() as group:
                group.create_task(produce())
                group.create_task(synthesize())
            if not spoken:
                raise RuntimeError('Ответ не содержит текста для озвучки.')
            await session.played.wait()
            if cancel.is_set():
                return
            answer = clean_speech(''.join(answer_parts))
            self.history.extend(({'role': 'user', 'content': text}, {'role': 'assistant', 'content': answer}))
            self.signals.dialog_done.emit(token)
        except asyncio.CancelledError:
            cancel.set()
        except Exception as error:
            cancel.set()
            while isinstance(error, ExceptionGroup):
                error = error.exceptions[0]
            message = str(error) if isinstance(error, RuntimeError) else 'Ошибка обработки голосового ответа.'
            LOGGER.error('stage=stream_failed token=%d error_type=%s', token, type(error).__name__)
            self.signals.failed.emit(token, message)
        finally:
            image_jpeg = None  # Screenshots belong only to this request, never history.
            while not sentences.empty():
                sentences.get_nowait()
            session.inflight.clear()
            session.pcm_bytes = 0
            session.played.set()
            if self.stream_session is session:
                self.stream_session = None

    def release_pcm(self, token: int, sequence: int) -> None:
        self._schedule(self._release_pcm, token, sequence)

    def _release_pcm(self, token: int, sequence: int) -> None:
        session = self.stream_session
        if session is None or session.token != token or sequence not in session.inflight:
            return
        session.pcm_bytes -= session.inflight.pop(sequence)
        session.credits.release()
        if not session.inflight:
            session.played.set()

    def cancel(self) -> None:
        with self.lock:
            self.cancel_generation += 1
            self.cancel_event.set()
            target = self.current
        self._schedule(self._cancel, target)

    def _cancel(self, target) -> None:
        if target is not None and not target.done() and not target.cancelling():
            target.cancel()

    def shutdown(self) -> None:
        with self.lock:
            self.shutdown_requested = True
        self.cancel()
        self._schedule(self._stop_loop)

    def _stop_loop(self) -> None:
        asyncio.get_running_loop().stop()
