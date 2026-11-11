"""PortAudio callback does no inference, UI calls or file I/O."""
from dataclasses import dataclass
import logging
from queue import Queue, Empty, Full
from threading import Event, Lock
from time import perf_counter

RATE = 16000
FRAME_SAMPLES = 512
FRAME_SECONDS = FRAME_SAMPLES / RATE
FRAME_BYTES = FRAME_SAMPLES * 2
LOGGER = logging.getLogger(__name__)


def validate_pcm_frame(pcm: bytes) -> None:
    if not isinstance(pcm, bytes) or len(pcm) != FRAME_BYTES:
        raise ValueError('Ожидался PCM16 mono кадр: 512 отсчётов / 1024 байта.')


def pcm_rms(pcm: bytes) -> float:
    """Normalized RMS (0..1); float conversion prevents int16 squaring overflow."""
    import numpy as np
    validate_pcm_frame(pcm)
    samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(samples * samples)))


@dataclass(frozen=True)
class InputDevice:
    index: int
    name: str
    host_api: str
    is_default: bool
    supports_pcm16: bool

    @property
    def label(self) -> str:
        suffix = ' — по умолчанию' if self.is_default else ''
        if not self.supports_pcm16:
            suffix += ' — нет PCM16 / 16 кГц / mono'
        return f'{self.name} · {self.host_api} · №{self.index}{suffix}'


def list_input_devices() -> list[InputDevice]:
    """Read-only enumeration/format checks; run outside the GUI thread."""
    import sounddevice as sd
    devices, host_apis = sd.query_devices(), sd.query_hostapis()
    default_index = int(sd.default.device[0])
    result = []
    for index, device in enumerate(devices):
        if device['max_input_channels'] < 1:
            continue
        try:
            sd.check_input_settings(device=index, samplerate=RATE, channels=1, dtype='int16')
            supported = True
        except Exception:
            supported = False
        result.append(InputDevice(index, device['name'], host_apis[device['hostapi']]['name'],
                                  index == default_index, supported))
    return result


@dataclass(frozen=True)
class AudioFrame:
    pcm: bytes
    captured_at: float  # frame end in perf_counter time domain


class FrameInbox:
    def __init__(self, capacity=32):
        self.queue = Queue(maxsize=capacity)
        self.damaged = Event()
        self.device_error = Event()
        self.revision_lock = Lock()
        self._revision = 0

    @property
    def revision(self):
        with self.revision_lock:
            return self._revision

    def mark_damaged(self):
        with self.revision_lock:
            self._revision += 1
            self.damaged.set()

    @property
    def size(self):
        return self.queue.qsize()

    def push(self, pcm, timestamp):
        try:
            self.queue.put_nowait(AudioFrame(pcm, timestamp))
        except Full:
            self.mark_damaged()

    def get(self, timeout=None):
        return self.queue.get(timeout=timeout)

    def clear(self):
        while True:
            try:
                self.queue.get_nowait()
            except Empty:
                break
        with self.revision_lock:
            self._revision += 1
            self.damaged.clear()


class AudioCapture:
    def __init__(self, inbox, device=None):
        self.inbox = inbox
        self.device = device
        self.stream = None

    def callback(self, indata, frames, time_info, status):
        pcm = bytes(indata)
        if status or frames != FRAME_SAMPLES or len(pcm) != FRAME_BYTES:
            self.inbox.mark_damaged()
            return
        self.inbox.push(pcm, perf_counter())

    def open(self):
        import sounddevice as sd
        try:
            sd.check_input_settings(device=self.device, channels=1, dtype='int16', samplerate=RATE)
            self.stream = sd.RawInputStream(
                device=self.device, samplerate=RATE, blocksize=FRAME_SAMPLES,
                channels=1, dtype='int16', callback=self.callback,
                finished_callback=self.inbox.device_error.set,
            )
            if (self.stream.samplerate != RATE or self.stream.channels != 1
                    or self.stream.dtype != 'int16'):
                raise ValueError('Unexpected stream format')
            self.stream.start()
            LOGGER.info('stage=input_open device=%s rate=%d channels=1 dtype=int16 frame_samples=%d',
                        self.stream.device, RATE, FRAME_SAMPLES)
        except Exception:
            self.close()
            raise RuntimeError('Микрофон недоступен или не поддерживает PCM16 / 16 кГц / mono.') from None

    def close(self):
        if self.stream is not None:
            stream, self.stream = self.stream, None
            try:
                stream.stop()
            finally:
                stream.close()
