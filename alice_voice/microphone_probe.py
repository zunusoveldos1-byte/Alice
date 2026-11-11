"""User-started, four-second RAM-only raw/processed comparison."""
from threading import Lock,Event,Thread
from queue import Empty
from time import perf_counter
import numpy as np
from .audio import AudioCapture,FrameInbox,pcm_rms
from .speech import PCM


class MicrophoneProbe:
    def __init__(self):
        self.lock=Lock();self.raw=[];self.clean=[];self.speech=0;self.clipped=0
        self.start=perf_counter();self.level=0.;self.error=''

    def raw_frame(self,frame):
        with self.lock:
            if frame.captured_at>=self.start and len(self.raw)<125:
                self.raw.append(frame.pcm)
                self.level=pcm_rms(frame.pcm)
                a=np.frombuffer(frame.pcm,dtype='<i2').astype(np.int32)
                self.clipped+=int(np.count_nonzero(np.abs(a)>=32700))

    def processed_frame(self,frame,probability):
        with self.lock:
            if frame.captured_at>=self.start and len(self.clean)<125:
                self.clean.append(frame.pcm);self.speech+=probability>=.5

    @property
    def ready(self):
        with self.lock:return len(self.raw)>=125 and len(self.clean)>=125

    def snapshot(self):
        with self.lock:
            return {'raw_frames':len(self.raw),'clean_frames':len(self.clean),'level':self.level,
                    'speech_frames':self.speech,'clipped':self.clipped,'error':self.error}

    def pcm(self,processed):
        with self.lock:return PCM(b''.join(self.clean if processed else self.raw),16000)

    def clear(self):
        with self.lock:self.raw.clear();self.clean.clear()


class ProbeWorker(Thread):
    def __init__(self,settings,probe):
        super().__init__(name='alice-mic-test',daemon=False)
        self.settings,self.probe=settings,probe;self.stop=Event()

    def run(self):
        capture=processor=None
        try:
            from .noise import MicrophoneProcessor
            from .vad import SileroVAD
            processor=MicrophoneProcessor(self.settings.noise_mode)
            vad=SileroVAD(self.settings.vad_model)
            inbox=FrameInbox();capture=AudioCapture(inbox,self.settings.input_device);capture.open()
            deadline=perf_counter()+8
            while not self.stop.is_set() and not self.probe.ready:
                if perf_counter()>deadline:raise RuntimeError('Не получены кадры микрофона. Проверьте устройство.')
                if inbox.damaged.is_set():raise RuntimeError('Потеря кадров теста. Повторите запись.')
                try:raw=inbox.get(timeout=.05)
                except Empty:continue
                self.probe.raw_frame(raw)
                for frame in processor.process(raw):self.probe.processed_frame(frame,vad(frame.pcm,16000))
        except Exception as error:
            self.probe.error=str(error) if isinstance(error,RuntimeError) else 'Проверка микрофона недоступна.'
        finally:
            if capture:capture.close()
            if processor:processor.close()
