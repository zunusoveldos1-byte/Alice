from collections import deque
from dataclasses import dataclass, field
import ctypes
import json
import logging
import math
import os
from pathlib import Path
import re
from uuid import uuid4
from time import perf_counter

from .audio import AudioFrame, FRAME_SECONDS, RATE, validate_pcm_frame, pcm_rms
from .vad import SileroVAD  # Compatibility for existing imports.

LOGGER = logging.getLogger(__name__)


def is_stop_command(text):
    normalized = re.sub(r'[^\w\s]', '', text.casefold())
    return ' '.join(normalized.split()) in {'отключайся', 'выключи микрофон', 'отключи микрофон'}


def vosk_path(path):
    resolved = str(Path(path).resolve())
    if not Path(resolved).is_dir():
        raise RuntimeError('Не найден каталог локальной русской модели Vosk.')
    if os.name == 'nt' and not resolved.isascii():
        buffer = ctypes.create_unicode_buffer(32768)
        function = ctypes.windll.kernel32.GetShortPathNameW
        function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        function.restype = ctypes.c_uint32
        length = function(resolved, buffer, len(buffer))
        if not length or length >= len(buffer) or not buffer.value.isascii() or not Path(buffer.value).is_dir():
            raise RuntimeError('Vosk требует ASCII-путь. Короткий NTFS-путь недоступен; перенесите модель, например в C:\\VoiceModels\\vosk-ru.')
        return buffer.value
    return resolved


class VoskRecognizer:
    def __init__(self, model_path=None, native=None):
        self.model = None
        if native is None:
            import vosk
            vosk.SetLogLevel(-1)
            try:
                self.model = vosk.Model(vosk_path(model_path))
                self.native = vosk.KaldiRecognizer(self.model, RATE)
            except RuntimeError:
                raise
            except Exception:
                raise RuntimeError('Не удалось загрузить модель Vosk; проверьте каталог и совместимость библиотеки.') from None
        else:
            self.native = native
        self.segments = []
        self.reset()

    def reset(self):
        self.segments.clear()
        self.last_rms = 0.0
        self.last_partial = ''
        self.next_partial_log = 0.0
        self.native.Reset()

    def feed(self, pcm):
        validate_pcm_frame(pcm)
        self.last_rms = pcm_rms(pcm)
        if self.native.AcceptWaveform(pcm):
            text = json.loads(self.native.Result()).get('text', '').strip()
            LOGGER.info('stage=stt_segment rms=%.6f text=%r', self.last_rms, text)
            if text:
                self.segments.append(text)
            return ' '.join(self.segments)
        partial = json.loads(self.native.PartialResult()).get('partial', '')
        now = perf_counter()
        if partial != self.last_partial and now >= self.next_partial_log:
            LOGGER.info('stage=stt_partial rms=%.6f text=%r', self.last_rms, partial)
            self.last_partial = partial
            self.next_partial_log = now + .1
        return ' '.join((*self.segments, partial)).strip()

    def finalize(self):
        try:
            text = json.loads(self.native.FinalResult()).get('text', '').strip()
            answer = ' '.join(self.segments + ([text] if text else []))
            LOGGER.info('stage=stt_final rms=%.6f text=%r', self.last_rms, answer)
            return answer
        finally:
            self.reset()


@dataclass
class PhraseUpdate:
    frames: list[AudioFrame] = field(default_factory=list)
    started: bool = False
    finished: bool = False
    discarded: bool = False
    utterance_id: str = ''
    speech_end: float = 0
    phrase_start: float = 0
    audio_start: float = 0


class PhraseSegmenter:
    def __init__(self, threshold=.5, silence_ms=650, preroll_ms=320, max_phrase_s=30,
                 min_speech_ms=320):
        self.threshold = threshold
        self.silence_frames = math.ceil(silence_ms / 32)
        self.max_frames = math.ceil(max_phrase_s / FRAME_SECONDS)
        self.min_speech_frames = math.ceil(min_speech_ms / 32)
        self.preroll = deque(maxlen=math.ceil(preroll_ms / 32))
        self.reset()

    def reset(self):
        self.preroll.clear()
        self.active = False
        self.confirmed = False
        self.pending: list[AudioFrame] = []
        self.voiced = 0
        self.silent = self.count = 0
        self.utterance_id = ''
        self.last_speech = self.phrase_start = self.audio_start = 0

    def feed(self, frame, probability):
        speech = probability >= self.threshold
        update = PhraseUpdate()
        if not self.active:
            if not speech:
                self.preroll.append(frame)
                return update
            self.active = True
            self.utterance_id = uuid4().hex
            self.phrase_start = frame.captured_at - FRAME_SECONDS
            first = self.preroll[0] if self.preroll else frame
            self.audio_start = first.captured_at - FRAME_SECONDS
            self.pending.extend(self.preroll)
            self.preroll.clear()
        if self.confirmed:
            update.frames.append(frame)
        else:
            self.pending.append(frame)
        self.count += 1
        if speech:
            self.voiced += 1
            self.last_speech = frame.captured_at
            self.silent = 0
        else:
            self.silent += 1
        if not self.confirmed and speech and self.voiced >= self.min_speech_frames:
            self.confirmed = True
            update.started = True
            update.frames, self.pending = self.pending, []
        update.utterance_id = self.utterance_id
        update.speech_end = self.last_speech
        update.phrase_start = self.phrase_start
        update.audio_start = self.audio_start
        if self.silent >= self.silence_frames or self.count >= self.max_frames:
            update.finished = self.confirmed
            update.discarded = not self.confirmed
            self.reset()
        return update
