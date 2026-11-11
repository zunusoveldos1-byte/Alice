"""Fixed, cancellable Windows desktop operations; model text is never a command."""
import asyncio
import hashlib
import io
import os
from pathlib import Path
import re
import subprocess

from .memory import check_cancel
from .windows_actions import Endpoint, WindowsActions


def _result(message, data=None, verified=False, status='done'):
    return dict(status=status, message=message, data=data or {}, verified=verified)


class DesktopBackend:
    SETTINGS = {'general': 'ms-settings:', 'sound': 'ms-settings:sound',
                'display': 'ms-settings:display', 'network': 'ms-settings:network',
                'bluetooth': 'ms-settings:bluetooth', 'apps': 'ms-settings:appsfeatures',
                'privacy': 'ms-settings:privacy', 'updates': 'ms-settings:windowsupdate'}
    MEDIA = {'play_pause': 0xB3, 'next': 0xB0, 'previous': 0xB1, 'stop': 0xB2}
    BLOCKED = {'cmd', 'powershell', 'powershell_ise', 'pwsh', 'python', 'pythonw',
               'py', 'wscript', 'cscript', 'mshta', 'rundll32', 'regsvr32', 'regedit',
               'wt', 'windowsterminal', 'bash', 'wsl', 'sh', 'node', 'perl', 'ruby',
               'conhost', 'mmc', 'msiexec', 'installutil', 'schtasks', 'taskkill',
               'shutdown', 'control', 'explorer', 'java', 'javaw', 'dotnet'}

    def __init__(self, settings):
        self.settings = settings
        self._catalog = None

    @staticmethod
    def _safe_executable(path):
        name = Path(path).stem.casefold()
        return (Path(path).suffix.casefold() == '.exe' and name not in DesktopBackend.BLOCKED
                and not re.match(r'^(python|powershell|pwsh|unins|uninstall|setup|install)', name))

    @staticmethod
    def _window(hwnd):
        import win32gui
        import win32process
        import win32api
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        exe = ''
        try:
            handle = win32api.OpenProcess(0x1000, False, pid)
            try:
                import ctypes
                from ctypes import wintypes
                query = ctypes.windll.kernel32.QueryFullProcessImageNameW
                query.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
                query.restype = wintypes.BOOL
                buffer = ctypes.create_unicode_buffer(32768)
                size = wintypes.DWORD(len(buffer))
                if query(int(handle), 0, buffer, ctypes.byref(size)):
                    exe = buffer.value
            finally:
                handle.Close()
        except Exception:
            pass  # Elevated processes can deny metadata access.
        import ctypes
        dpi = ctypes.windll.user32.SetThreadDpiAwarenessContext
        dpi.argtypes = [ctypes.c_void_p]
        dpi.restype = ctypes.c_void_p
        previous = dpi(ctypes.c_void_p(-4))
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        finally:
            if previous:
                dpi(previous)
        return dict(hwnd=int(hwnd), title=win32gui.GetWindowText(hwnd), exe=exe, pid=pid,
                    rect=[left, top, right-left, bottom-top])

    @staticmethod
    def foreground_window_id():
        """Cheap focus-change probe, without process handles or window metadata."""
        import win32gui
        return int(win32gui.GetForegroundWindow() or 0)

    def active_window(self):
        hwnd = self.foreground_window_id()
        return self._window(hwnd) if hwnd else dict(hwnd=0, title='', exe='', pid=0, rect=[0, 0, 0, 0])

    def list_windows(self):
        import win32gui
        windows = []
        def visit(hwnd, unused):
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
                try:
                    windows.append(self._window(hwnd))
                except Exception:
                    pass  # A window can disappear during enumeration.
        win32gui.EnumWindows(visit, None)
        return windows

    def _shortcut_target(self, path):
        if path.suffix.casefold() == '.url':
            # Only a fixed numeric Steam game launch protocol is admitted.
            content = path.read_text(encoding='utf-8-sig', errors='replace')
            match = re.search(r'^URL=(steam://rungameid/[0-9]+)\s*$', content, re.M | re.I)
            return match.group(1) if match else None
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        shortcut = None
        try:
            shortcut = win32com.client.Dispatch('WScript.Shell').CreateShortcut(str(path))
            target = os.path.expandvars(shortcut.TargetPath)
            # Do not admit shortcuts carrying arbitrary scripts/command arguments.
            if shortcut.Arguments.strip() or not self._safe_executable(target):
                return None
            return target if Path(target).is_file() else None
        finally:
            shortcut = None
            pythoncom.CoUninitialize()

    def _build_catalog(self):
        import winreg
        catalog = {}
        def add(name, path, kind='exe'):
            identity = hashlib.sha256(str(path).casefold().encode('utf-8')).hexdigest()[:20]
            catalog[identity] = dict(name=name, id=identity, path=str(path), kind=kind)
        root = Path(os.environ.get('WINDIR', r'C:\Windows'))
        for name, path in [('Блокнот', root/'System32'/'notepad.exe'),
                           ('Калькулятор', root/'System32'/'calc.exe'),
                           ('Проводник', root/'explorer.exe')]:
            if path.is_file():
                add(name, path, 'fixed')
        starts = [Path(os.environ.get('APPDATA', ''))/'Microsoft/Windows/Start Menu/Programs',
                  Path(os.environ.get('PROGRAMDATA', r'C:\ProgramData'))/'Microsoft/Windows/Start Menu/Programs']
        for folder in starts:
            if not folder.is_dir():
                continue
            for path in folder.rglob('*'):
                if path.suffix.casefold() not in ('.lnk', '.url'):
                    continue
                try:
                    target = self._shortcut_target(path)
                    if target:
                        add(path.stem, path, 'shortcut')
                except Exception:
                    continue
        for hive in [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]:
            for view in [winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY]:
                try:
                    with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths', 0, winreg.KEY_READ | view) as key:
                        for i in range(winreg.QueryInfoKey(key)[0]):
                            child = winreg.EnumKey(key, i)
                            try:
                                with winreg.OpenKey(key, child) as subkey:
                                    path = os.path.expandvars(winreg.QueryValueEx(subkey, '')[0]).strip('"')
                                if self._safe_executable(path) and Path(path).is_file():
                                    add(Path(child).stem, path)
                            except OSError:
                                continue
                except OSError:
                    continue
        self._catalog = catalog

    def list_apps(self):
        if self._catalog is None:
            self._build_catalog()
        return sorted([dict(name=app['name'], id=app['id']) for app in self._catalog.values()],
                      key=lambda item: item['name'].casefold())

    @staticmethod
    def _match(items, name, fields):
        name = name.strip().casefold()
        exact = [item for item in items if any(str(item.get(f, '')).casefold() == name for f in fields)]
        return exact or ([item for item in items if any(name in str(item.get(f, '')).casefold() for f in fields)] if name else [])

    def _guard(self, cancel, target_hwnd=0):
        check_cancel(cancel)
        if target_hwnd and self.active_window()['hwnd'] != target_hwnd:
            return _result('Активное окно изменилось. Повторите команду для нужного окна.', status='clarification')
        return None

    def execute(self, name, args, cancel, target_hwnd=0):
        try:
            check_cancel(cancel)
            return self._execute(name, args, cancel, target_hwnd)
        except asyncio.CancelledError:
            return _result('Действие отменено.', status='cancelled')
        except (ValueError, KeyError, TypeError):
            return _result('Недопустимые параметры инструмента.', status='error')
        except Exception:
            return _result('Windows не выполнила действие. Возможны ограничения доступа или оборудования.', status='error')

    def _execute(self, name, args, cancel, target_hwnd):
        if name == 'launch_app':
            self.list_apps()
            matches = self._match(self._catalog.values(), args['name'], ('id', 'name'))
            if len(matches) != 1:
                return _result('Уточните приложение.', {'choices': [dict(name=x['name'], id=x['id']) for x in matches]}, status='clarification')
            app = matches[0]
            if app.get('kind') == 'shortcut' and not self._shortcut_target(Path(app['path'])):
                return _result('Ярлык больше не поддерживается.', status='error')
            check_cancel(cancel)
            os.startfile(app['path'])
            return _result('Windows приняла запрос запуска: ' + app['name'] + '.', {'app': app['name']})
        if name == 'window_control':
            mode = args['mode']
            if mode not in ('activate', 'minimize', 'maximize', 'restore', 'close'):
                raise ValueError()
            guard = self._guard(cancel, target_hwnd)
            if guard:
                return guard
            if args.get('window_id'):
                matches = [row for row in self.list_windows() if row['hwnd'] == args['window_id']]
                if len(matches) != 1:
                    return _result('Выбранное окно уже закрыто. Выберите окно заново.', status='cancelled')
                hwnd = matches[0]['hwnd']
            elif args.get('name'):
                matches = self._match(self.list_windows(), args['name'], ('title', 'exe'))
                if len(matches) != 1:
                    return _result('Уточните окно.', {'choices': matches}, status='clarification')
                hwnd = matches[0]['hwnd']
            else:
                hwnd = target_hwnd or self.active_window()['hwnd']
            if not hwnd:
                return _result('Нет активного окна.', status='clarification')
            guard = self._guard(cancel, target_hwnd)
            return guard or self._window_mutation(hwnd, mode, cancel, target_hwnd)
        if name == 'volume':
            mode = args['mode']
            if mode not in ('set', 'up', 'down', 'mute', 'unmute', 'status'):
                raise ValueError()
            with Endpoint() as endpoint:
                if mode in ('set', 'up', 'down'):
                    value = args.get('value', 10)
                    if type(value) is not int or not 0 <= value <= 100:
                        raise ValueError()
                    target = value if mode == 'set' else max(0, min(100, endpoint.level() + (value if mode == 'up' else -value)))
                    check_cancel(cancel)
                    endpoint.set_level(target)
                    if abs(endpoint.level() - target) > 1:
                        return _result('Windows не подтвердила громкость.', status='error')
                elif mode in ('mute', 'unmute'):
                    check_cancel(cancel)
                    endpoint.set_mute(mode == 'mute')
                    if endpoint.muted() != (mode == 'mute'):
                        return _result('Windows не подтвердила состояние звука.', status='error')
                level, muted = endpoint.level(), endpoint.muted()
                return _result(f'Громкость Windows {level} процентов.' + (' Звук выключен.' if muted else ''), {'level': level, 'muted': muted}, True)
        if name == 'windows_settings':
            uri = self.SETTINGS[args['page']]
            check_cancel(cancel)
            os.startfile(uri)
            return _result('Windows приняла запрос открытия параметров.')
        if name == 'media':
            import win32api
            key = self.MEDIA[args['command']]
            check_cancel(cancel)
            win32api.keybd_event(key, 0, 0, 0)
            win32api.keybd_event(key, 0, 2, 0)
            return _result('Медиакоманда передана Windows.')
        if name == 'brightness':
            return self._brightness(args['value'], cancel)
        if name == 'screenshot':
            return self._screenshot(args['target'], cancel, target_hwnd)
        if name == 'user_folder':
            from win32com.shell import shell, shellcon
            folder = {'downloads': '{374DE290-123F-4565-9164-39C4925E467B}',
                      'documents': shellcon.CSIDL_PERSONAL, 'pictures': shellcon.CSIDL_MYPICTURES}[args['name']]
            if isinstance(folder, str):
                from pywintypes import IID
                path = shell.SHGetKnownFolderPath(IID(folder), 0, None)
            else:
                path = shell.SHGetFolderPath(0, folder, None, 0)
            check_cancel(cancel)
            os.startfile(path)
            return _result('Windows приняла запрос открытия папки.')
        if name == 'desktop':
            WindowsActions().desktop(cancel)
            return _result('Запрос показа рабочего стола передан Windows.')
        if name == 'lock':
            WindowsActions().lock(cancel)
            return _result('Запрос блокировки экрана передан Windows.')
        if name == 'power':
            flag = {'shutdown': '/s', 'restart': '/r'}[args['action']]
            command = str(Path(os.environ.get('WINDIR', r'C:\Windows'))/'System32'/'shutdown.exe')
            check_cancel(cancel)
            subprocess.Popen([command, flag, '/t', '0'], shell=False, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return _result('Запрос питания передан Windows.')
        raise ValueError()

    def _window_mutation(self, hwnd, mode, cancel, target_hwnd=0):
        import win32gui
        if not win32gui.IsWindow(hwnd):
            return _result('Окно уже закрыто.', status='clarification')
        guard = self._guard(cancel, target_hwnd)
        if guard:
            return guard
        if mode == 'close':
            win32gui.PostMessage(hwnd, 0x0010, 0, 0)  # WM_CLOSE preserves save dialogs.
            return _result('Окну передан запрос закрытия; может потребоваться сохранить файл.')
        if mode == 'activate':
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, 9)
            guard = self._guard(cancel, target_hwnd)
            if guard:
                return guard
            win32gui.SetForegroundWindow(hwnd)
            verified = win32gui.GetForegroundWindow() == hwnd
        else:
            win32gui.ShowWindow(hwnd, {'minimize': 6, 'maximize': 3, 'restore': 9}[mode])
            verified = bool(win32gui.IsIconic(hwnd)) if mode == 'minimize' else win32gui.GetWindowPlacement(hwnd)[1] == (3 if mode == 'maximize' else 1)
        return _result('Состояние окна изменено.' if verified else 'Windows не подтвердила состояние окна.', verified=verified, status='done' if verified else 'error')

    def _brightness(self, value, cancel):
        if type(value) is not int or not 0 <= value <= 100:
            raise ValueError()
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        service, methods, monitor = None, [], None
        try:
            service = win32com.client.GetObject(r'winmgmts:\\.\root\WMI')
            methods = list(service.ExecQuery('SELECT * FROM WmiMonitorBrightnessMethods WHERE Active=True'))
            if not methods:
                return _result('Этот монитор не поддерживает изменение яркости через Windows WMI. Используйте кнопки монитора.', status='error')
            for monitor in methods:
                check_cancel(cancel)
                monitor.WmiSetBrightness(0, value)
            actual = [int(x.CurrentBrightness) for x in service.ExecQuery('SELECT * FROM WmiMonitorBrightness WHERE Active=True')]
            verified = bool(actual) and all(abs(x-value) <= 1 for x in actual)
            return _result('Яркость установлена.' if verified else 'Windows приняла запрос яркости, проверка недоступна.', {'brightness': actual}, verified)
        except Exception:
            return _result('Изменение яркости недоступно для этого оборудования через Windows WMI.', status='error')
        finally:
            monitor, methods, service = None, [], None
            pythoncom.CoUninitialize()

    def _screenshot(self, target, cancel, target_hwnd):
        from PIL import ImageGrab
        import win32clipboard
        if target not in ('screen', 'active'):
            raise ValueError()
        bbox = None
        if target == 'active':
            guard = self._guard(cancel, target_hwnd)
            if guard:
                return guard
            window = self.active_window()
            if target_hwnd and window['hwnd'] != target_hwnd:
                return _result('Активное окно изменилось. Повторите запрос снимка.', status='clarification')
            if not window['hwnd']:
                return _result('Нет активного окна.', status='clarification')
            target_hwnd = window['hwnd']
            left, top, width, height = window['rect']
            bbox = (left, top, left+width, top+height)
        check_cancel(cancel)
        image = ImageGrab.grab(bbox=bbox, all_screens=True)
        guard = self._guard(cancel, target_hwnd if target == 'active' else 0)
        if guard:
            return guard
        stream = io.BytesIO()
        image.convert('RGB').save(stream, format='BMP')
        payload = stream.getvalue()[14:]  # Clipboard CF_DIB excludes BITMAPFILEHEADER.
        win32clipboard.OpenClipboard()
        try:
            guard = self._guard(cancel, target_hwnd if target == 'active' else 0)
            if guard:
                return guard
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_DIB, payload)
            verified = win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_DIB)
        finally:
            win32clipboard.CloseClipboard()
        return _result('Снимок скопирован в буфер обмена.', {'width': image.width, 'height': image.height, 'clipboard': True}, bool(verified))
