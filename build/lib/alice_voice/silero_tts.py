"""CPU Silero v4_ru. Complete audio per text fragment, not native streaming."""
import asyncio
import hashlib
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from time import monotonic

from .speech import PCM, MAX_PCM_BYTES

MODEL = 'v4_ru'
VOICES = ('aidar', 'baya', 'kseniya', 'xenia', 'eugene')
MODEL_URL = 'https://models.silero.ai/models/tts/ru/v4_ru.pt'
MODEL_SHA256 = '896ab96347d5bd781ab97959d4fd6885620e5aab52405d3445626eb7c1414b00'
SAMPLE_RATE = 24000


def _check(cancel):
    if cancel.is_set():
        raise asyncio.CancelledError


def _download(path, cancel):
    import httpx
    deadline = monotonic() + 300
    size = 0
    with httpx.Client(trust_env=False, timeout=httpx.Timeout(5, connect=10)) as client:
        with client.stream('GET', MODEL_URL) as response:
            response.raise_for_status()
            with path.open('wb') as file:
                for data in response.iter_bytes(65536):
                    _check(cancel)
                    size += len(data)
                    if size > 128 * 1024 * 1024 or monotonic() > deadline:
                        raise RuntimeError('Загрузка Silero превысила ограничение размера или времени.')
                    file.write(data)
                file.flush()
                os.fsync(file.fileno())


def _load_model(path, threads):
    try:
        import torch
    except ImportError:
        raise RuntimeError('Для Silero установите CPU PyTorch из requirements-silero.txt.') from None
    torch.set_num_threads(threads)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass  # PyTorch allows this only before the first parallel operation.
    model = torch.package.PackageImporter(str(path)).load_pickle('tts_models', 'model')
    model.to(torch.device('cpu'))
    if not set(VOICES).issubset(set(model.speakers)):
        raise RuntimeError('Модель Silero несовместима с выбранными голосами.')
    class CPUModel:
        def apply_tts(self, **kwargs):
            with torch.inference_mode():
                return model.apply_tts(**kwargs)
    return CPUModel()


class SileroModelCache:
    def __init__(self, directory=None, *, downloader=None, loader=None, expected_sha256=MODEL_SHA256):
        self.directory = Path(directory) if directory is not None else Path('models/silero-tts')
        self.downloader = downloader or _download
        self.loader = loader or _load_model
        self.expected_sha256 = expected_sha256
        self.lock = RLock()
        self.models = {}

    def _valid(self, path, cancel):
        digest = hashlib.sha256()
        with path.open('rb') as file:
            while data := file.read(1024 * 1024):
                _check(cancel)
                digest.update(data)
        return digest.hexdigest() == self.expected_sha256

    def get(self, threads, cancel, status=lambda value: None):
        with self.lock:
            _check(cancel)
            if threads in self.models:
                status('silero_ready')
                return self.models[threads]
            path = self.directory / (MODEL + '.pt')
            temporary = None
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    status('silero_download')
                    with NamedTemporaryFile(dir=self.directory, prefix=MODEL + '-', suffix='.part', delete=False) as file:
                        temporary = Path(file.name)
                    self.downloader(temporary, cancel)
                    _check(cancel)
                    if not self._valid(temporary, cancel):
                        raise RuntimeError('Загруженная модель Silero повреждена: SHA-256 не совпадает.')
                    os.replace(temporary, path)
                    temporary = None
                status('silero_loading')
                if not self._valid(path, cancel):
                    raise RuntimeError('Кэш модели Silero повреждён. Удалите models/silero-tts/v4_ru.pt и повторите загрузку.')
            except asyncio.CancelledError:
                raise
            except RuntimeError:
                raise
            except Exception:
                raise RuntimeError('Не удалось загрузить Silero или открыть локальный кэш. '
                                   'Проверьте интернет при первой загрузке, место и права записи.') from None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            try:
                _check(cancel)
                model = self.loader(path, threads)
                _check(cancel)
            except asyncio.CancelledError:
                raise
            except RuntimeError as error:
                if str(error).startswith('Для Silero установите'):
                    raise
                raise RuntimeError('Не удалось открыть модель Silero. Проверьте CPU PyTorch и целостность кэша.') from None
            except Exception:
                raise RuntimeError('Не удалось открыть модель Silero. Проверьте CPU PyTorch и целостность кэша.') from None
            self.models.clear()  # Retain one copy, including after runtime reconfiguration.
            self.models[threads] = model
            status('silero_ready')
            return model


_CACHE = SileroModelCache()


def text_fragments(text, limit=240):
    current = []
    for word in text.split():
        if len(word) > limit:
            raise RuntimeError('Слишком длинное слово для Silero. Разбейте текст на короткие фразы.')
        if current and len(' '.join(current)) + len(word) + 1 > limit:
            yield ' '.join(current)
            current = []
        current.append(word)
    if current:
        yield ' '.join(current)


class SileroSynthesizer:
    accepts_fragments = True

    def __init__(self, settings, executor, *, cache=None):
        self.settings, self.executor = settings, executor
        self.cache = cache or _CACHE

    async def _work(self, function, cancel):
        _check(cancel)
        try:
            async with asyncio.timeout(self.settings.timeout_s):
                result = await asyncio.get_running_loop().run_in_executor(self.executor, function)
            _check(cancel)
            return result
        except asyncio.CancelledError:
            cancel.set()
            raise
        except TimeoutError:
            cancel.set()
            raise RuntimeError('Истёк таймаут Silero. Модель выполняется на CPU; текущий результат будет отброшен.') from None

    async def prepare(self, cancel, status=lambda value: None):
        await self._work(lambda: self.cache.get(self.settings.silero_threads, cancel, status), cancel)

    def _synthesize(self, text, cancel):
        import numpy as np
        with self.cache.lock:
            model = self.cache.get(self.settings.silero_threads, cancel)
            _check(cancel)
            try:
                audio = model.apply_tts(text=text, speaker=self.settings.silero_voice, sample_rate=SAMPLE_RATE)
                _check(cancel)
                if hasattr(audio, 'detach'):
                    audio = audio.detach().cpu().numpy()
                audio = np.asarray(audio)
                if audio.ndim != 1 or not audio.size or audio.size * 2 > MAX_PCM_BYTES or not np.isfinite(audio).all():
                    raise RuntimeError('Модель Silero вернула некорректный или слишком большой PCM.')
                data = np.rint(np.clip(audio, -1, 1) * 32767).astype('<i2').tobytes()
                _check(cancel)
                return PCM(data, SAMPLE_RATE)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise RuntimeError('Не удалось получить PCM Silero. Проверьте текст, выбранный голос и ресурсы CPU.') from None

    async def synthesize(self, text, cancel):
        if not text.strip() or len(text) > 240:
            raise RuntimeError('Silero ожидает непустой текстовый фрагмент до 240 символов.')
        return await self._work(lambda: self._synthesize(text, cancel), cancel)

    async def stream_pcm(self, text, cancel):
        """Sequential complete fragments; the model API itself is not streaming."""
        for fragment in text_fragments(text):
            _check(cancel)
            yield await self.synthesize(fragment, cancel)
