"""Fixed Windows adapters. Never pass recognized text to a shell."""
import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import subprocess
import uuid
from .memory import check_cancel


class _GUID(c.Structure):
    _fields_ = [('data', c.c_ubyte * 16)]

    def __init__(self, text):
        super().__init__((c.c_ubyte * 16).from_buffer_copy(uuid.UUID(text).bytes_le))


def _call(ptr, index, types, *values):
    table = c.cast(ptr, c.POINTER(c.POINTER(c.c_void_p))).contents
    result = c.WINFUNCTYPE(c.c_long, c.c_void_p, *types)(table[index])(ptr, *values)
    if result < 0:
        raise RuntimeError('Windows отклонила команду звука.')


class Endpoint:
    def __enter__(self):
        self.pointers = []
        hr = c.windll.ole32.CoInitializeEx(None, 0)
        self.initialized = hr >= 0
        if not self.initialized:
            raise RuntimeError('Не удалось подготовить Windows Audio.')
        try:
            enumerator = c.c_void_p()
            hr = c.windll.ole32.CoCreateInstance(c.byref(_GUID('bcde0395-e52f-467c-8e3d-c4579291692e')),
                None, 23, c.byref(_GUID('a95664d2-9614-4f35-a746-de8db63617e6')), c.byref(enumerator))
            if hr < 0:
                raise RuntimeError('Windows Audio недоступно.')
            self.pointers.append(enumerator)
            device = c.c_void_p()
            _call(enumerator, 4, [c.c_int, c.c_int, c.POINTER(c.c_void_p)], 0, 1, c.byref(device))
            self.pointers.append(device)
            self.volume = c.c_void_p()
            _call(device, 3, [c.POINTER(_GUID), w.DWORD, c.c_void_p, c.POINTER(c.c_void_p)],
                  c.byref(_GUID('5cdf2c82-841e-4546-9722-0cf74078229a')), 23, None, c.byref(self.volume))
            self.pointers.append(self.volume)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *unused):
        for ptr in reversed(self.pointers):
            _call(ptr, 2, [])
        if self.initialized:
            c.windll.ole32.CoUninitialize()

    def level(self):
        value = c.c_float()
        _call(self.volume, 9, [c.POINTER(c.c_float)], c.byref(value))
        return round(value.value * 100)

    def muted(self):
        value = w.BOOL()
        _call(self.volume, 15, [c.POINTER(w.BOOL)], c.byref(value))
        return bool(value.value)

    def set_level(self, value):
        _call(self.volume, 7, [c.c_float, c.c_void_p], value / 100, None)

    def set_mute(self, value):
        _call(self.volume, 14, [w.BOOL, c.c_void_p], value, None)


class WindowsActions:
    APPS = {'блокнот': 'notepad.exe', 'калькулятор': 'calc.exe', 'проводник': 'explorer.exe'}

    def open_app(self, name, cancel=None):
        executable = self.APPS[name]
        root = Path(os.environ.get('WINDIR', r'C:\Windows'))
        path = root / executable if executable == 'explorer.exe' else root / 'System32' / executable
        if not path.is_file():
            raise RuntimeError('Это приложение Windows не найдено.')
        check_cancel(cancel)
        subprocess.Popen([str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, shell=False)
        return f'Windows приняла запрос запуска: {name}.'

    def settings(self, page, cancel=None):
        check_cancel(cancel)
        os.startfile({'sound': 'ms-settings:sound', 'general': 'ms-settings:'}[page])
        return 'Открываю параметры Windows.'

    def desktop(self, cancel=None):
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            shell = win32com.client.Dispatch('Shell.Application')
            check_cancel(cancel)
            shell.MinimizeAll()
            shell = None
        finally:
            pythoncom.CoUninitialize()
        return 'Окна свёрнуты.'

    def audio(self, mode, value=None, cancel=None):
        with Endpoint() as endpoint:
            check_cancel(cancel)
            if mode == 'mute':
                endpoint.set_mute(value)
                if endpoint.muted() != value:
                    raise RuntimeError('Windows не подтвердила изменение звука.')
            elif mode in ('set', 'up', 'down'):
                target = value if mode == 'set' else max(0, min(100, endpoint.level() + (10 if mode == 'up' else -10)))
                endpoint.set_level(target)
                if abs(endpoint.level() - target) > 1:
                    raise RuntimeError('Windows не подтвердила новую громкость.')
            return f'Громкость Windows {endpoint.level()} процентов.' + (' Звук выключен.' if endpoint.muted() else '')

    def lock(self, cancel=None):
        check_cancel(cancel)
        if not c.windll.user32.LockWorkStation():
            raise RuntimeError('Windows отклонила блокировку экрана.')
        return 'Запрос блокировки экрана передан Windows.'
