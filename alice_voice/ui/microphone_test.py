from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QDialog,QVBoxLayout,QPushButton,QLabel,QProgressBar
from ..microphone_probe import MicrophoneProbe,ProbeWorker
from ..playback import PCMPlayback


class MicrophoneTestDialog(QDialog):
    def __init__(self,settings,controller=None,parent=None):
        super().__init__(parent)
        self.settings,self.controller=settings,controller
        self.probe=None;self.worker=None;self.tapped=None;self.closing=False
        self.setWindowTitle('Проверка микрофона · 4 секунды в памяти')
        self.resize(560,330)
        layout=QVBoxLayout(self)
        self.status=QLabel('Нажмите «Записать» и говорите 4 секунды. '
            'Проверяйте разговоры других людей только с их согласия. Звук остаётся в RAM до закрытия окна.')
        self.status.setWordWrap(True);layout.addWidget(self.status)
        self.level=QProgressBar();self.level.setRange(0,100);layout.addWidget(self.level)
        self.record=QPushButton('Записать 4 секунды');self.record.clicked.connect(self.start);layout.addWidget(self.record)
        self.original=QPushButton('Прослушать исходный');self.original.clicked.connect(lambda:self.listen(False));layout.addWidget(self.original)
        self.processed=QPushButton('Прослушать обработанный');self.processed.clicked.connect(lambda:self.listen(True));layout.addWidget(self.processed)
        self.original.setEnabled(False);self.processed.setEnabled(False)
        close=QPushButton('Закрыть и удалить запись из памяти');close.clicked.connect(self.close);layout.addWidget(close)
        self.playback=PCMPlayback(self)
        self.timer=QTimer(self);self.timer.setInterval(80);self.timer.timeout.connect(self.poll);self.timer.start()

    def start(self):
        if self.worker and self.worker.is_alive():return
        self.playback.stop()
        if self.controller:
            self.controller.stop_actions();self.controller.testing_microphone=True
        self.probe=MicrophoneProbe()
        capture=getattr(self.controller,'capture',None)
        # Use the existing stream only if it has the requested device and processing mode.
        if capture is not None and capture.is_alive():
            if capture.settings.input_device!=self.settings.input_device or capture.settings.noise_mode!=self.settings.noise_mode:
                self.status.setText('Сначала сохраните выбор микрофона и шумоподавления, затем повторите тест.')
                self.controller.testing_microphone=False;return
            self.tapped=capture;capture.probe=self.probe
        else:
            self.worker=ProbeWorker(self.settings,self.probe);self.worker.start()
        self.record.setEnabled(False);self.original.setEnabled(False);self.processed.setEnabled(False)

    def poll(self):
        if self.closing:
            if not self.worker or not self.worker.is_alive():self.close()
            return
        if self.probe is None:return
        data=self.probe.snapshot();self.level.setValue(min(100,round(data['level']*200)))
        if data['error']:
            self.status.setText(data['error']);self.record.setEnabled(True);return
        self.status.setText(f"Записано {data['raw_frames']*.032:.1f} с. Речь: {'есть' if data['speech_frames'] else 'не обнаружена'}. "
                            + ('Перегрузка: уменьшите усиление микрофона.' if data['clipped']>200 else 'Автоматическое усиление выключено.'))
        if self.probe.ready:
            if self.tapped:self.tapped.probe=None
            self.record.setEnabled(True);self.original.setEnabled(True);self.processed.setEnabled(True)

    def listen(self,processed):
        if self.probe and self.probe.ready:
            self.playback.play(self.probe.pcm(processed),self.settings.volume,self.settings.output_device)

    def closeEvent(self,event):
        if self.tapped:self.tapped.probe=None;self.tapped.reset_phrase()
        if self.worker and self.worker.is_alive():
            self.worker.stop.set();self.closing=True;event.ignore();return
        self.timer.stop();self.playback.close()
        if self.probe:self.probe.clear()
        if self.controller:
            self.controller.testing_microphone=False
            self.controller.accept_after=__import__('time').perf_counter()+.3
        event.accept()

    def reject(self):
        self.close()
