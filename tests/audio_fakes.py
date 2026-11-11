"""Native push-sink test double shared by audio regression tests."""
from PyQt6.QtMultimedia import QAudio


class Signal:
    def connect(self, slot): self.slot = slot
    def disconnect(self, slot): self.slot = None
    def emit(self, value):
        if self.slot: self.slot(value)


class PushSink:
    """Native push buffer: accepting bytes does not mean they were played."""
    def __init__(self, rate=16000):
        self.stateChanged = Signal()
        self.rate, self.written, self.played = rate, 0, 0
        self.capacity, self.reset_count, self.start_count = 6400, 0, 0
        self.received = bytearray()
        self.status = QAudio.State.StoppedState
    def setBufferSize(self, size): self.requested_buffer_size = size
    def setVolume(self, value): pass
    def start(self):
        self.start_count += 1
        self.status = QAudio.State.IdleState
        self.stateChanged.emit(self.status)
        return self
    def write(self, data):
        data = bytes(data)
        count = min(len(data), self.bytesFree())
        self.received.extend(data[:count])
        self.written += count
        self.status = QAudio.State.ActiveState
        self.stateChanged.emit(self.status)
        return count
    def bytesFree(self): return max(0, self.capacity - self.written + self.played)
    def bufferSize(self): return self.capacity
    def processedUSecs(self): return self.played * 1000000 // (self.rate * 2)
    def state(self): return self.status
    def error(self): return QAudio.Error.NoError
    def reset(self):
        self.reset_count += 1
        self.written = self.played = 0
        self.status = QAudio.State.StoppedState
    def deleteLater(self): pass
    def suspend(self): self.status = QAudio.State.SuspendedState
    def resume(self): self.status = QAudio.State.ActiveState
    def advance(self, count):
        self.played = min(self.written, self.played + count)
        if self.played == self.written:
            self.status = QAudio.State.IdleState
            self.stateChanged.emit(self.status)


