"""Windows generic credentials; no JSON, environment or keyring fallback."""
from threading import RLock

TARGET = 'AliceVoice/GeminiAPI'
ELEVENLABS_TARGET = 'AliceVoice/ElevenLabsAPI'
LMSTUDIO_TARGET = 'AliceVoice/LMStudioToken'
_LOCK = RLock()


class CredentialError(RuntimeError):
    """Safe user-facing message, never a native exception or a secret."""


class MissingCredentialError(CredentialError):
    pass


def _missing(error):
    return getattr(error, 'winerror', None) == 1168 or (bool(error.args) and error.args[0] == 1168)


def _load_key(target, label):
    with _LOCK:
        try:
            import win32cred
            blob = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC)['CredentialBlob']
            value = blob.decode('utf-16-le') if isinstance(blob, bytes) else blob
            if not isinstance(value, str):
                raise TypeError
        except Exception as error:
            if _missing(error):
                raise MissingCredentialError(f'Ключ {label} отсутствует. Добавьте его в настройках.') from None
            raise CredentialError(f'Не удалось прочитать ключ {label} из Windows Credential Manager. '
                                  'Проверьте доступ к хранилищу Windows.') from None
        if not value.strip():
            raise MissingCredentialError(f'Ключ {label} отсутствует. Добавьте его в настройках.')
        return value.strip()


def _save_key(target, label, key):
    value = key.strip()
    if not value or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError(f'Введите непустой ключ {label} без пробелов и управляющих символов.')
    with _LOCK:
        try:
            import win32cred
            win32cred.CredWrite({
                'Type': win32cred.CRED_TYPE_GENERIC, 'TargetName': target,
                'UserName': label, 'CredentialBlob': value,
                'Persist': win32cred.CRED_PERSIST_LOCAL_MACHINE,
            }, 0)
            if _load_key(target, label) != value:
                raise CredentialError('credential readback mismatch')
        except Exception:
            raise CredentialError(f'Не удалось сохранить и проверить ключ {label} '
                                  'в Windows Credential Manager. Повторите сохранение.') from None


def _delete_key(target, label):
    with _LOCK:
        try:
            import win32cred
            win32cred.CredDelete(target, win32cred.CRED_TYPE_GENERIC, 0)
        except Exception as error:
            if _missing(error):
                return
            raise CredentialError(f'Не удалось удалить ключ {label} '
                                  'из Windows Credential Manager. Повторите удаление.') from None
        try:
            _load_key(target, label)
        except MissingCredentialError:
            return
        except CredentialError:
            raise CredentialError(f'Не удалось проверить удаление ключа {label} '
                                  'из Windows Credential Manager.') from None
        raise CredentialError(f'Не удалось подтвердить удаление ключа {label} '
                              'из Windows Credential Manager.')


def save_gemini_key(key):
    _save_key(TARGET, 'Gemini', key)


def load_gemini_key():
    return _load_key(TARGET, 'Gemini')


def delete_gemini_key():
    _delete_key(TARGET, 'Gemini')


def save_elevenlabs_key(key):
    _save_key(ELEVENLABS_TARGET, 'ElevenLabs', key)


def load_elevenlabs_key():
    return _load_key(ELEVENLABS_TARGET, 'ElevenLabs')


def delete_elevenlabs_key():
    _delete_key(ELEVENLABS_TARGET, 'ElevenLabs')


def save_lmstudio_token(token):
    _save_key(LMSTUDIO_TARGET, 'LM Studio', token)


def load_lmstudio_token():
    return _load_key(LMSTUDIO_TARGET, 'LM Studio')


def delete_lmstudio_token():
    _delete_key(LMSTUDIO_TARGET, 'LM Studio')
