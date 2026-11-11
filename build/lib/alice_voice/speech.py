import asyncio
from dataclasses import dataclass
import re
from time import monotonic
from typing import Protocol

MAX_COMPRESSED_BYTES = 8 * 1024 * 1024
MAX_PCM_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class PCM:
    data: bytes
    sample_rate: int
    channels: int = 1

    def __post_init__(self):
        if self.sample_rate <= 0 or self.channels not in (1, 2) or len(self.data) % (2 * self.channels) or not self.data or len(self.data) > MAX_PCM_BYTES:
            raise ValueError('Неверный или слишком большой PCM-буфер.')


def clean_speech(text):
    text = re.sub(r'```[^\n]*\n?', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'(?m)^\s{0,3}(?:#{1,6}\s+|[-*]\s+|>\s*)', '', text)
    text = text.replace('**', '').replace('__', '').replace('`', '')
    return ' '.join(text.split())


class SpeechSynthesizer(Protocol):
    async def synthesize(self, text: str, cancel) -> PCM: ...


async def collect_edge_audio(stream, cancel):
    data = bytearray()
    async for chunk in stream:
        if cancel.is_set():
            raise asyncio.CancelledError
        if chunk['type'] == 'audio':
            if len(data) + len(chunk['data']) > MAX_COMPRESSED_BYTES:
                raise RuntimeError('Ответ Edge-TTS превысил ограничение буфера.')
            data.extend(chunk['data'])
    if not data:
        raise RuntimeError('Edge-TTS не вернул аудио.')
    return bytes(data)


def decode_edge(data, cancel=None):
    import miniaudio
    stream = miniaudio.stream_memory(data, output_format=miniaudio.SampleFormat.SIGNED16,
                                    nchannels=1, sample_rate=24000, frames_to_read=4096)
    pcm = bytearray()
    try:
        for samples in stream:
            if cancel is not None and cancel.is_set():
                raise asyncio.CancelledError
            chunk = samples.tobytes()
            if len(pcm) + len(chunk) > MAX_PCM_BYTES:
                raise RuntimeError('Декодированный ответ превысил ограничение PCM-буфера.')
            pcm.extend(chunk)
    finally:
        stream.close()
    return PCM(bytes(pcm), 24000)


class EdgeSynthesizer:
    def __init__(self, voice, executor, timeout_s=60):
        self.voice = voice
        self.executor = executor
        self.timeout = timeout_s

    async def synthesize(self, text, cancel):
        import edge_tts
        try:
            async with asyncio.timeout(self.timeout):
                data = await collect_edge_audio(edge_tts.Communicate(text, self.voice).stream(), cancel)
                loop = asyncio.get_running_loop()
                pcm = await loop.run_in_executor(self.executor, decode_edge, data, cancel)
                if cancel.is_set():
                    raise asyncio.CancelledError
                return pcm
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise RuntimeError('Истёк таймаут Edge-TTS.') from None
        except Exception:
            raise RuntimeError('Не удалось получить или декодировать Edge-TTS. Проверьте интернет и выбранный голос.') from None


def sapi_synthesize(text, voice_name, cancel, timeout_s):
    """Create, use and release all COM objects on this same executor thread."""
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    voice = memory = selected = token = None
    try:
        voice = win32com.client.Dispatch('SAPI.SpVoice')
        memory = win32com.client.Dispatch('SAPI.SpMemoryStream')
        # SpeechAudioFormatType.SAFT22kHz16BitMono = 22
        memory.Format.Type = 22
        if voice_name:
            selected = None
            for token in voice.GetVoices():
                if token.GetDescription() == voice_name:
                    selected = token
                    break
            if selected is None:
                raise RuntimeError('Выбранный голос SAPI не установлен.')
            voice.Voice = selected
            selected = token = None
        voice.AudioOutputStream = memory
        voice.Speak(text, 1)  # SVSFlagsAsync
        deadline = monotonic() + timeout_s
        while not voice.WaitUntilDone(20):
            pythoncom.PumpWaitingMessages()
            if cancel.is_set() or monotonic() >= deadline:
                voice.Speak('', 3)  # Async | PurgeBeforeSpeak
                if cancel.is_set():
                    raise asyncio.CancelledError
                raise RuntimeError('Истёк таймаут синтеза SAPI.')
        if cancel.is_set():
            raise asyncio.CancelledError
        data = bytes(memory.GetData())
        return PCM(data, 22050)
    except asyncio.CancelledError:
        raise
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError('Ошибка Windows SAPI. Проверьте установленный голос и аудиокомпоненты Windows.') from None
    finally:
        voice = memory = selected = token = None
        pythoncom.CoUninitialize()


class SAPISynthesizer:
    accepts_fragments = True
    def __init__(self, voice, executor, timeout_s=60):
        self.voice = voice
        self.executor = executor
        self.timeout = timeout_s

    async def synthesize(self, text, cancel):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, sapi_synthesize, text, self.voice, cancel, self.timeout)
