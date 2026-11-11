from dataclasses import asdict, dataclass, fields
import json
import logging
import math
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from typing import Any
from .secrets import save_elevenlabs_key


class SettingsMigrationError(RuntimeError):
    """Safe startup error; the original file is preserved on failure."""

SYSTEM_PROMPT = (
    'Ты русскоязычный голосовой помощник. Отвечай естественно и кратко, '
    'обычно одним–тремя предложениями. Используй законченные фразы, удобные '
    'для озвучки. Не добавляй Markdown, таблицы, код и длинные списки без '
    'прямой просьбы. Не утверждай, что выполнил действия: у тебя нет '
    'инструментов управления компьютером. Если информации недостаточно, '
    'задай один короткий уточняющий вопрос.'
)


@dataclass
class Settings:
    vad_model: str = 'models/silero_vad.onnx'
    vosk_model: str = 'models/vosk-model-small-ru-0.22'
    input_device: int | None = None
    output_device: str = ''
    threshold: float = 0.5
    min_speech_ms: int = 320
    silence_ms: int = 650
    preroll_ms: int = 320
    max_phrase_s: float = 30
    provider: str = 'ollama'
    ollama_url: str = 'http://127.0.0.1:11434'
    ollama_model: str = 'qwen3:4b'
    gemini_model: str = 'gemini-2.5-flash'
    lmstudio_url: str = 'http://localhost:1234/v1'
    lmstudio_model: str = ''
    timeout_s: float = 60
    history_turns: int = 6
    max_tokens: int = 256
    max_response_chars: int = 1200
    synthesizer: str = 'sapi'
    sapi_voice: str = ''
    silero_model: str = 'v4_ru'
    silero_voice: str = 'xenia'
    silero_threads: int = 2
    edge_voice: str = 'ru-RU-SvetlanaNeural'
    elevenlabs_voice_id: str = ''
    elevenlabs_model_id: str = 'eleven_multilingual_v2'
    volume: float = 0.8
    headphones: bool = False
    reduced_motion: bool = False
    system_prompt: str = SYSTEM_PROMPT

    def validate(self) -> 'Settings':
        # bool is a subclass of int: exact types prevent True becoming a count.
        for field in fields(self):
            value = getattr(self, field.name)
            expected = field.type
            if expected is float:
                valid = type(value) in (int, float)
                if valid:
                    try:
                        valid = math.isfinite(value)
                    except OverflowError:
                        valid = False
            elif expected == (int | None):
                valid = value is None or (type(value) is int and value >= 0)
            else:
                valid = type(value) is expected
            if not valid:
                raise ValueError(f'Неверный тип или значение настройки «{field.name}».')
        if not 0 < self.threshold < 1:
            raise ValueError('Порог VAD должен быть между 0 и 1.')
        if not 0 <= self.min_speech_ms <= 1000:
            raise ValueError('Минимум речи должен быть от 0 до 1000 мс (0 отключает фильтр).')
        if not 32 <= self.silence_ms <= 3000 or not 0 <= self.preroll_ms <= 1000:
            raise ValueError('Недопустимые настройки тишины или преролла.')
        if not 1 <= self.max_phrase_s <= 120 or not 1 <= self.timeout_s <= 180:
            raise ValueError('Недопустимый таймаут или максимальная длина фразы.')
        if self.provider not in ('ollama', 'gemini', 'lmstudio') or self.synthesizer not in ('sapi', 'edge', 'elevenlabs', 'silero'):
            raise ValueError('Неизвестный провайдер.')
        if self.silero_model != 'v4_ru' or self.silero_voice not in ('aidar', 'baya', 'kseniya', 'xenia', 'eugene'):
            raise ValueError('Выберите поддерживаемую модель и голос Silero.')
        if not 1 <= self.silero_threads <= 4:
            raise ValueError('Silero: число потоков CPU должно быть от 1 до 4.')
        if not 0 <= self.history_turns <= 20 or not 16 <= self.max_tokens <= 2048:
            raise ValueError('Недопустимый размер истории или ответа.')
        if not 32 <= self.max_response_chars <= 5000 or not 0 <= self.volume <= 1:
            raise ValueError('Недопустимая длина ответа или громкость.')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', self.gemini_model):
            raise ValueError('Имя модели Gemini должно содержать только буквы, цифры, точку, дефис.')
        if self.elevenlabs_voice_id and not re.fullmatch(r'[A-Za-z0-9_-]+', self.elevenlabs_voice_id):
            raise ValueError('Неверный Voice ID ElevenLabs.')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', self.elevenlabs_model_id):
            raise ValueError('Неверный Model ID ElevenLabs.')
        if not self.ollama_model.strip() or any(ord(c) < 32 for c in self.ollama_model):
            raise ValueError('Укажите имя выбранной модели Ollama без управляющих символов.')
        from urllib.parse import urlsplit
        try:
            lm_url = urlsplit(self.lmstudio_url)
            lm_url.port
        except ValueError:
            raise ValueError('Неверный URL LM Studio.') from None
        if (lm_url.scheme not in ('http', 'https') or not lm_url.hostname or lm_url.username or
                lm_url.password or lm_url.query or lm_url.fragment or
                not lm_url.path.rstrip('/').endswith('/v1') or any(ord(c) < 33 for c in self.lmstudio_url)):
            raise ValueError('URL LM Studio должен оканчиваться на /v1 и не содержать токен, пароль или query-параметры.')
        if len(self.lmstudio_model) > 512 or any(ord(c) < 32 for c in self.lmstudio_model):
            raise ValueError('Неверный ID модели LM Studio.')
        try:
            url = urlsplit(self.ollama_url)
            url.port  # Validate malformed/non-numeric ports before HTTP startup.
        except ValueError:
            raise ValueError('Неверный URL Ollama.') from None
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('URL Ollama не должен содержать ключи, пароль или query-параметры.')
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> 'Settings':
        if not isinstance(data, dict) or any(type(key) is not str for key in data):
            raise ValueError('Настройки должны быть JSON-объектом.')
        allowed = {f.name for f in fields(cls)}
        if set(data) - allowed:
            raise ValueError('Настройки содержат неизвестные поля.')
        try:
            return cls(**data).validate()
        except (TypeError, AttributeError):
            raise ValueError('Неверный формат настроек.') from None

    @classmethod
    def load(cls, path: str | Path) -> 'Settings':
        file = Path(path)
        if not file.exists():
            return cls()
        source = file.read_bytes()
        data = json.loads(source.decode('utf-8'))
        if not isinstance(data, dict) or 'elevenlabs_api_key' not in data:
            return cls.from_dict(data)
        legacy = data.pop('elevenlabs_api_key')
        if not isinstance(legacy, str):
            raise ValueError('Неверный формат старого ключа ElevenLabs; файл не изменён.')
        settings = cls.from_dict(data)  # Validate before changing the credential.
        if legacy.strip():
            try:
                save_elevenlabs_key(legacy)  # Includes independent readback.
            except (RuntimeError, ValueError):
                raise SettingsMigrationError('Не удалось перенести ключ ElevenLabs в Windows Credential Manager. '
                                             'Исходный файл настроек не изменён, ключ не удалён. '
                                             'Проверьте доступ к хранилищу Windows и повторите запуск.') from None
        try:
            settings._save_atomic(file, expected_source=source)
        except Exception:
            raise SettingsMigrationError('Не удалось очистить старое поле ElevenLabs в настройках. '
                                         'Исходный файл не изменён. Если ключ был непустым, '
                                         'его запись в Windows Credential Manager уже проверена. '
                                         'Проверьте доступ к файлу и повторите запуск.') from None
        return settings

    def save(self, path: str | Path) -> None:
        """Never discard an unmigrated credential during an ordinary save."""
        self.validate()
        destination = Path(path).absolute()
        try:
            source = destination.read_bytes()
        except FileNotFoundError:
            source = None
        if source is not None:
            data = json.loads(source.decode('utf-8'))
            if isinstance(data, dict) and data.get('elevenlabs_api_key'):
                raise SettingsMigrationError('Сначала перенесите старый ключ ElevenLabs: повторите запуск приложения. '
                                             'Файл настроек не изменён.')
        self._save_atomic(destination, expected_source=source)

    def _save_atomic(self, path: str | Path, *, expected_source=None) -> None:
        """Flush a complete JSON file, then atomically replace on the same volume."""
        self.validate()
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2, allow_nan=False)
        destination = Path(path).absolute()
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(mode='w', encoding='utf-8', dir=destination.parent,
                                    prefix=f'.{destination.name}.', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # Windows requires the temporary handle to be closed before replace.
            try:
                current = destination.read_bytes()
            except FileNotFoundError:
                current = None
            if current != expected_source:
                raise OSError('Файл настроек изменился во время сохранения. Повторите действие.')
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    logging.getLogger(__name__).warning('stage=settings_temp_cleanup_failed')
