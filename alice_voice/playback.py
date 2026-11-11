from time import perf_counter
from collections import deque
from PyQt6.QtCore import QObject, QByteArray, QBuffer, QIODevice, QTimer, pyqtSignal
from PyQt6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QMediaDevices


class PCMPlayback(QObject):
    started = pyqtSignal(float)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sink = None
        self.buffer = None
        self.storage = None
        self.paused = False
        self.first_active = False
        self.state_slot = None
        self._cached_sink = None
        self._format_key = None
        self._writer = None
        self._generation = 0
        self._packets = deque()
        self._enqueued = self._written = 0
        self._pumping = False
        self._timer = QTimer(self)
        self._timer.setInterval(5)
        self._timer.timeout.connect(self._pump)

    def play(self, pcm, volume=.8, device_name=''):
        if self._packets:
            self.stop()
        fmt = QAudioFormat()
        fmt.setSampleRate(pcm.sample_rate)
        fmt.setChannelCount(pcm.channels)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        devices = QMediaDevices.audioOutputs()
        device = QMediaDevices.defaultAudioOutput()
        if device_name:
            matches = [d for d in devices if d.description() == device_name]
            if not matches:
                raise RuntimeError('Выбранное устройство воспроизведения недоступно.')
            device = matches[0]
        if device.isNull() or not device.isFormatSupported(fmt):
            raise RuntimeError('Выходное устройство не поддерживает формат синтезированного PCM.')
        key = (device.id(), pcm.sample_rate, pcm.channels)
        if key != self._format_key:
            self.close()
            self._format_key = key
        # Open source before start(); stateChanged may fire reentrantly.
        self._append_packet(pcm)
        if self.sink is None:
            self.sink = self._cached_sink or QAudioSink(device, fmt, self)
            self._cached_sink = None
            self.sink.setBufferSize(pcm.sample_rate * pcm.channels * 2 // 10)
            self._generation += 1
            generation, sink = self._generation, self.sink
            self.state_slot = lambda state: self._state(state, sink, generation)
            sink.stateChanged.connect(self.state_slot)
            sink.setVolume(volume)
            self._writer = sink.start()
            if self._writer is None:
                self.stop()
                raise RuntimeError('Не удалось открыть поток PCM-выхода.')
        self.sink.setVolume(volume)
        self._timer.start()
        self._pump()

    def _append_packet(self, pcm):
        if sum(len(packet['data']) for packet in self._packets) + len(pcm.data) > 32 * 1024 * 1024:
            raise RuntimeError('Очередь PCM-выхода превысила ограничение.')
        storage = QByteArray(pcm.data)
        buffer = QBuffer(storage, self)
        if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            buffer.deleteLater()
            raise RuntimeError('Не удалось открыть PCM-буфер для чтения.')
        packet = {'buffer': buffer, 'storage': storage, 'data': pcm.data,
                  'start': self._enqueued, 'end': self._enqueued + len(pcm.data), 'started': False}
        self._enqueued = packet['end']
        self._packets.append(packet)
        if len(self._packets) == 1:
            self.buffer, self.storage = buffer, storage

    def append(self, pcm):
        """Prefetch matching-format PCM without resetting the native stream."""
        if self.sink is None or self._format_key[1:] != (pcm.sample_rate, pcm.channels):
            return False
        self._append_packet(pcm)
        self._timer.start()
        return True

    def _start_front(self):
        if self._packets and not self._packets[0]['started']:
            self._packets[0]['started'] = True
            self.started.emit(perf_counter())

    def _pump(self):
        if self._pumping or self.sink is None or self._writer is None or self.paused:
            return
        self._pumping = True
        sink, generation = self.sink, self._generation
        try:
            for packet in tuple(self._packets):
                source = packet['buffer']
                while not source.atEnd():
                    free = sink.bytesFree()
                    if free <= 0:
                        break
                    data = source.peek(min(free, 65536))
                    written = self._writer.write(data)
                    if written < 0:
                        raise RuntimeError('Ошибка записи в PCM-выход.')
                    if not written:
                        break
                    source.seek(source.pos() + written)
                    self._written += written
                if not source.atEnd():
                    break
            processed = sink.processedUSecs() * self._format_key[1] * self._format_key[2] * 2 // 1000000
            while self._packets:
                packet = self._packets[0]
                if (processed > packet['start'] or (processed >= packet['start']
                        and sink.state() == QAudio.State.ActiveState)) and self._written > packet['start']:
                    self._start_front()
                if self.sink is not sink or self._generation != generation:
                    return
                rendered = processed >= packet['end']
                drained = sink.state() == QAudio.State.IdleState and self._written == self._enqueued
                if not packet['started'] or not packet['buffer'].atEnd() or not (rendered or drained):
                    break
                self._packets.popleft()
                packet['buffer'].close()
                packet['buffer'].deleteLater()
                self.buffer = self._packets[0]['buffer'] if self._packets else None
                self.storage = self._packets[0]['storage'] if self._packets else None
                self.finished.emit()
                if self.sink is not sink or self._generation != generation:
                    return
            if not self._packets:
                self._timer.stop()
        except Exception:
            self.stop()
            self.error.emit('Ошибка PCM-воспроизведения. Проверьте выходное устройство.')
        finally:
            self._pumping = False

    def _state(self, state, source=None, generation=None):
        if (self.sink is None or (source is not None and source is not self.sink)
                or (generation is not None and generation != self._generation)):
            return
        if state == QAudio.State.ActiveState:
            if self._packets and self._written > self._packets[0]['start']:
                self._start_front()
        elif state == QAudio.State.StoppedState and self.sink.error() != QAudio.Error.NoError:
            sink, serial = self.sink, self._generation
            QTimer.singleShot(0, lambda: self._complete(sink, True, serial))

    def _complete(self, sink, failed: bool, generation=None) -> None:
        if self.sink is not sink or (generation is not None and generation != self._generation):
            return
        self.stop()
        if failed:
            self.error.emit('Ошибка PCM-воспроизведения. Проверьте выходное устройство.')
        else:
            self.finished.emit()

    def pause(self):
        if self.sink is not None and not self.paused:
            self.sink.suspend()
            self.paused = True

    def resume(self):
        if self.sink is not None and self.paused:
            self.sink.resume()
            self.paused = False
            self._pump()

    def output_level(self):
        """RMS of the 20ms PCM window currently reaching the audio device."""
        if self.sink is None or self.paused or not self._format_key:
            return 0.0
        import numpy as np
        rate, channels = self._format_key[1:]
        position = self.sink.processedUSecs() * rate * channels * 2 // 1000000
        for packet in self._packets:
            if packet['start'] <= position < packet['end']:
                offset = int(position - packet['start']) // 2 * 2
                data = packet['data'][offset:offset + max(2, rate * channels // 50 * 2)]
                samples = np.frombuffer(data[:len(data)//2*2], dtype='<i2').astype(np.float32)
                return min(1.0, float(np.sqrt(np.mean(samples*samples))) / 32768 * 4 * self.sink.volume()) if samples.size else 0.0
        return 0.0

    def set_volume(self, volume):
        if self.sink is not None:
            self.sink.setVolume(volume)

    def stop(self):
        self._timer.stop()
        self._generation += 1
        if self.sink is not None:
            sink, self.sink = self.sink, None
            sink.stateChanged.disconnect(self.state_slot)
            self.state_slot = None
            sink.reset()  # Discard queued device PCM immediately; do not drain.
            self._cached_sink = sink
        for packet in self._packets:
            packet['buffer'].close()
            packet['buffer'].deleteLater()
        if self.buffer is not None and not self._packets:
            self.buffer.close()
            self.buffer.deleteLater()
        self._packets.clear()
        self._writer = None
        self._enqueued = self._written = 0
        self.buffer = self.storage = None
        self.paused = False

    def close(self):
        self.stop()
        if self._cached_sink is not None:
            self._cached_sink.deleteLater()
            self._cached_sink = None
        self._format_key = None


class EchoGuard:
    """Timestamp interval rejection, NOT acoustic echo cancellation."""
    def __init__(self, tail_seconds=.25):
        from collections import deque
        self.intervals = deque(maxlen=64)
        self.active_start = None
        self.tail_seconds = tail_seconds

    def start(self, when):
        self.active_start = when

    def end(self, when):
        if self.active_start is not None:
            self.intervals.append((self.active_start, when + self.tail_seconds))
            self.active_start = None

    def overlaps(self, start, end):
        if self.active_start is not None and end >= self.active_start:
            return True
        return any(start <= right and end >= left for left, right in self.intervals)
