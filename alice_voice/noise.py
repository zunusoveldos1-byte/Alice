"""One stateful RNNoise and two streaming resamplers per capture, outside UI."""
import logging
import ctypes
from pathlib import Path
from threading import Lock
import numpy as np
from .audio import AudioFrame,FRAME_SECONDS,validate_pcm_frame

_LIB=None
_LIB_LOCK=Lock()


def native_library():
    global _LIB
    with _LIB_LOCK:
        if _LIB is None:
            path=Path(__file__).resolve().parents[1]/'models'/'rnnoise'/'rnnoise.dll'
            library=ctypes.CDLL(str(path))
            pointer=ctypes.POINTER(ctypes.c_float)
            library.rnnoise_create.argtypes=[ctypes.c_void_p];library.rnnoise_create.restype=ctypes.c_void_p
            library.rnnoise_destroy.argtypes=[ctypes.c_void_p];library.rnnoise_destroy.restype=None
            library.rnnoise_process_frame.argtypes=[ctypes.c_void_p,pointer,pointer]
            library.rnnoise_process_frame.restype=ctypes.c_float
            library.rnnoise_get_frame_size.restype=ctypes.c_int
            if library.rnnoise_get_frame_size()!=480:raise OSError('Unexpected RNNoise ABI')
            _LIB=library
        return _LIB


class MicrophoneProcessor:
    def __init__(self,mode='off',*,process_native=None):
        self.mode=mode
        self.state=None
        self.native=process_native
        self.high=np.empty(0,dtype=np.float32)
        self.low=np.empty(0,dtype=np.float32)
        self.origin=None
        self.samples=0
        if mode!='off':
            try:
                import soxr
                if self.native is None:
                    library=native_library()
                    self.state=library.rnnoise_create(None)
                    if not self.state:raise OSError('RNNoise allocation failed')
                    self.destroy=library.rnnoise_destroy
                    def process(frame):
                        signal=frame.astype(np.float32)
                        pointer=signal.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
                        library.rnnoise_process_frame(self.state,pointer,pointer)
                        return signal
                    self.native=process
                self.up=soxr.ResampleStream(16000,48000,1,dtype='float32',quality='LQ')
                self.down=soxr.ResampleStream(48000,16000,1,dtype='float32',quality='LQ')
            except (ImportError,OSError) as error:
                self.close()
                raise RuntimeError('RNNoise недоступен. Установите requirements-fair.txt и запустите scripts/prepare_rnnoise.py либо выберите «Выключено».') from error
            logging.getLogger(__name__).info('stage=rnnoise_initialized rate=48000 samples=480 channels=1')

    @property
    def pending_samples(self):return len(self.high)+len(self.low)

    def process(self,frame):
        validate_pcm_frame(frame.pcm)
        if self.mode=='off':return [frame]
        if self.origin is None:self.origin=frame.captured_at-FRAME_SECONDS
        signal=np.frombuffer(frame.pcm,dtype='<i2').astype(np.float32)
        self.high=np.concatenate((self.high,self.up.resample_chunk(signal)))
        output=[]
        while len(self.high)>=480:
            chunk=self.high[:480];self.high=self.high[480:]
            clean=np.asarray(self.native(np.clip(np.rint(chunk),-32768,32767).astype(np.int16)),dtype=np.float32)
            if clean.shape!=(480,) or not np.isfinite(clean).all():raise RuntimeError('RNNoise вернул неверный кадр.')
            self.low=np.concatenate((self.low,self.down.resample_chunk(clean)))
        while len(self.low)>=512:
            pcm=np.clip(np.rint(self.low[:512]),-32768,32767).astype('<i2').tobytes()
            self.low=self.low[512:];self.samples+=512
            output.append(AudioFrame(pcm,self.origin+self.samples/16000))
        if self.pending_samples>8192:raise RuntimeError('Буфер RNNoise переполнен. Фраза отменена.')
        return output

    def reset(self):
        self.high=np.empty(0,dtype=np.float32);self.low=np.empty(0,dtype=np.float32)
        self.origin=None;self.samples=0
        if self.mode!='off':self.up.clear();self.down.clear()

    def close(self):
        if self.state is not None:
            self.destroy(self.state);self.state=None
