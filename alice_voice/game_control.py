"""Conservative, local, observed game-menu control. No screenshots are persisted.

Observer protocol: observe(window) -> {hwnd, rect:[physical left,top,w,h],
timestamp:monotonic seconds, tokens:[{text,rect:[capture-relative x,y,w,h]}]}.
Inputter protocol: click(x,y,guard), release_all(). Guard MUST be invoked immediately
before sending input. Dependencies are optional; unavailable OCR returns a manual step.
"""
from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
import re
import threading
import time


class _Stopped(Exception):
    pass


class _Unsafe(Exception):
    pass


_LABELS = {
    'settings': {'settings', 'options', 'настройки', 'параметры'},
    'general': {'general', 'общие', 'основные'},
    'graphics': {'graphics', 'video', 'графика', 'видео'},
    'controls': {'controls', 'управление'},
    'audio': {'audio', 'sound', 'звук', 'аудио'},
    'mouse_sensitivity': {'mouse sensitivity', 'чувствительность мыши'},
    'music_volume': {'music volume', 'громкость музыки', 'музыка'},
    'resolution': {'resolution', 'screen resolution', 'разрешение', 'разрешение экрана'},
    'display_mode': {'display mode', 'window mode', 'режим экрана', 'режим отображения'},
    'back': {'back', 'назад', 'return', 'вернуться'},
}
_GOALS = {'open_settings', 'inspect_parameter', 'change_parameter', 'back'}
_PARAMETERS = {'mouse_sensitivity', 'music_volume', 'resolution', 'display_mode'}


def _norm(text):
    return re.sub(r'\s+', ' ', str(text).casefold().strip().rstrip(':'))


def _matches(tokens, label):
    return [t for t in tokens if _norm(t['text']) in _LABELS[label]]


def _state(tokens):
    words = {_norm(t['text']) for t in tokens}
    if words & {'pause', 'paused', 'пауза'}:
        return 'pause'
    if _matches(tokens, 'settings') and any(_matches(tokens, c) for c in ('graphics', 'audio', 'controls', 'general')):
        return 'settings'
    if any(_matches(tokens, p) for p in _PARAMETERS):
        return 'settings'
    if words & {'main menu', 'главное меню'}:
        return 'menu'
    return 'unknown'  # Absence of a menu is not evidence of gameplay.


def _category_visible(tokens, category):
    """A navigation tab alone is not evidence its destination is open."""
    content = {'graphics': ('resolution', 'display_mode'),
               'audio': ('music_volume',), 'controls': ('mouse_sensitivity',)}
    return category == 'general' or any(_matches(tokens, p) for p in content.get(category, (category,)))


_INPUT_LOCK = threading.RLock()
_HELD_LEFT = False


def _send_mouse(flags):
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('mouseData', wintypes.DWORD),
                    ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]
    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD), ('dwFlags', wintypes.DWORD),
                    ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]
    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [('uMsg', wintypes.DWORD), ('wParamL', wintypes.WORD), ('wParamH', wintypes.WORD)]
    class PAYLOAD(ctypes.Union):
        _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT), ('hi', HARDWAREINPUT)]
    class INPUT(ctypes.Structure):
        _anonymous_ = ('payload',)
        _fields_ = [('type', wintypes.DWORD), ('payload', PAYLOAD)]
    event = INPUT(type=0, mi=MOUSEINPUT(dwFlags=flags))
    api = ctypes.windll.user32.SendInput
    api.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    api.restype = wintypes.UINT
    if api(1, ctypes.byref(event), ctypes.sizeof(INPUT)) != 1:
        raise PermissionError('Windows refused ordinary SendInput')


def release_all_inputs():
    """Release only mouse input held by this module; never release other apps' keys."""
    global _HELD_LEFT
    with _INPUT_LOCK:
        if _HELD_LEFT:
            _send_mouse(0x0004)
            _HELD_LEFT = False


class WindowsInputter:
    def click(self, x, y, guard):
        global _HELD_LEFT
        with _INPUT_LOCK:
            guard()
            # OCR coordinates come from mss physical pixels, irrespective of Qt DPI.
            if not ctypes.windll.user32.SetPhysicalCursorPos(int(x), int(y)):
                raise PermissionError('Windows refused cursor positioning')
            guard()
            _send_mouse(0x0002)
            _HELD_LEFT = True
            try:
                _send_mouse(0x0004)
                _HELD_LEFT = False
            finally:
                release_all_inputs()

    def release_all(self):
        release_all_inputs()


class LocalOCRObserver:
    """mss captures physical screen pixels; Windows OCR stays on this device."""
    def __init__(self, ocr_timeout=3.0):
        self.ocr_timeout = float(ocr_timeout)
        if not 0 < self.ocr_timeout <= 10:
            raise ValueError('OCR timeout must be between zero and ten seconds')

    def observe(self, window):
        import mss
        # Set thread scope only; do not alter the application's global DPI mode.
        user32 = ctypes.windll.user32
        dpi = user32.SetThreadDpiAwarenessContext
        dpi.argtypes = [ctypes.c_void_p]
        dpi.restype = ctypes.c_void_p
        old = dpi(ctypes.c_void_p(-4))
        try:
            rect = wintypes.RECT()
            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            user32.GetWindowRect.restype = wintypes.BOOL
            if not user32.GetWindowRect(int(window['hwnd']), ctypes.byref(rect)):
                raise _Unsafe('Не удалось определить границы окна.')
            bounds = [rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top]
            if bounds[2] <= 0 or bounds[3] <= 0:
                raise _Unsafe('Окно свёрнуто или недоступно.')
            with mss.mss() as capture:
                shot = capture.grab(dict(zip(('left', 'top', 'width', 'height'), bounds)))
                pixels = bytes(shot.bgra)
            timestamp = time.monotonic()
        finally:
            if old:
                dpi(old)
        tokens = asyncio.run(self._recognize(pixels, bounds[2], bounds[3]))
        return {'hwnd': window['hwnd'], 'rect': bounds, 'timestamp': timestamp, 'tokens': tokens}

    async def _recognize(self, pixels, width, height):
        from winrt.runtime import ApartmentType, init_apartment, uninit_apartment
        # Worker threads do not inherit the GUI thread's COM apartment.
        init_apartment(ApartmentType.MULTI_THREADED)
        try:
            return await self._recognize_initialized(pixels, width, height)
        finally:
            uninit_apartment()

    async def _recognize_initialized(self, pixels, width, height):
        from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter
        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise _Unsafe('Локальный OCR для установленных языков недоступен.')
        if max(width, height) > OcrEngine.max_image_dimension:
            raise _Unsafe('Размер окна превышает предел локального OCR.')
        bitmap = SoftwareBitmap(BitmapPixelFormat.BGRA8, width, height, BitmapAlphaMode.IGNORE)
        writer = DataWriter()
        try:
            writer.write_bytes(pixels)
            bitmap.copy_from_buffer(writer.detach_buffer())
            result = await asyncio.wait_for(engine.recognize_async(bitmap), timeout=self.ocr_timeout)
            tokens = []
            for line in result.lines:
                words = list(line.words)
                if not words:
                    continue
                boxes = [w.bounding_rect for w in words]
                left = min(b.x for b in boxes); top = min(b.y for b in boxes)
                right = max(b.x + b.width for b in boxes); bottom = max(b.y + b.height for b in boxes)
                # Keep lines for multiword labels, but numeric and +/- words separately.
                text = line.text
                special = [w for w in words if re.fullmatch(r'(?:\d+(?:[.,]\d+)?%?|[+−-])', w.text.strip())]
                label_words = [w.text for w in words if w not in special]
                label = ' '.join(label_words)
                if special and label and any(_norm(label) in labels for labels in _LABELS.values()):
                    label_boxes = [w.bounding_rect for w in words if w not in special]
                    l = min(b.x for b in label_boxes); t = min(b.y for b in label_boxes)
                    r = max(b.x+b.width for b in label_boxes); b = max(b.y+b.height for b in label_boxes)
                    tokens.append({'text': label, 'rect': [l,t,r-l,b-t]})
                    for word in special:
                        b = word.bounding_rect
                        tokens.append({'text': word.text, 'rect': [b.x,b.y,b.width,b.height]})
                else:
                    tokens.append({'text': text, 'rect': [left,top,right-left,bottom-top]})
            return tokens
        finally:
            bitmap.close()
            writer.close()


class GameController:
    def __init__(self, settings, desktop, observer=None, inputter=None):
        self.settings = settings
        self.desktop = desktop
        self.observer = observer if observer is not None else LocalOCRObserver()
        self.inputter = inputter if inputter is not None else WindowsInputter()

    @staticmethod
    def _result(status, message, data=None, verified=False):
        return {'status': status, 'message': message, 'data': data or {}, 'verified': verified}

    def _guard(self, cancel, hwnd, observation=None):
        if cancel.is_set():
            raise _Stopped()
        for permission in ('permission_screen', 'permission_input', 'permission_games'):
            enabled = self.settings.get(permission, False) if isinstance(self.settings, dict) else getattr(self.settings, permission, False)
            if not enabled:
                raise PermissionError('Требуются разрешения на экран, ввод и игры.')
        window = self.desktop.active_window()
        if not window or window.get('hwnd') != hwnd:
            raise _Unsafe('Активное окно изменилось. Верните нужную игру и повторите команду.')
        if observation:
            if observation.get('hwnd') != hwnd or not 0 <= time.monotonic() - observation['timestamp'] <= 3:
                raise _Unsafe('Наблюдение устарело. Повторите команду.')
            if list(window.get('rect', [])) != list(observation['rect']):
                raise _Unsafe('Окно переместилось. Нужен новый снимок перед вводом.')
        return window

    def _observe(self, cancel, hwnd):
        window = self._guard(cancel, hwnd)
        observation = self.observer.observe(window)
        self._guard(cancel, hwnd, observation)
        bounds = observation['rect']
        for token in observation['tokens']:
            x,y,w,h = token['rect']
            if w <= 0 or h <= 0 or x < 0 or y < 0 or x+w > bounds[2] or y+h > bounds[3]:
                raise _Unsafe('OCR вернул элемент за границами окна.')
        return observation

    def _click(self, token, observation, cancel, hwnd):
        x,y,w,h = token['rect']
        left,top,_,_ = observation['rect']
        guard = lambda: self._guard(cancel, hwnd, observation)
        guard()
        self.inputter.click(round(left+x+w/2), round(top+y+h/2), guard)

    @staticmethod
    def _row(tokens, label):
        _,y,_,h = label['rect']
        cy = y+h/2
        return [t for t in tokens if t is not label and abs(t['rect'][1]+t['rect'][3]/2-cy) <= max(8,h*.7)]

    def run(self, goal, category, value, cancel, target_hwnd, notify=None, direction='set'):
        data = {}
        target_value = value
        try:
            if goal not in _GOALS or category not in set(_LABELS)-{'settings','back'}:
                return self._result('error', 'Неподдерживаемая команда управления игрой.')
            if direction not in {'set', 'up', 'down'}:
                return self._result('error', 'Неподдерживаемое направление изменения параметра.')
            if goal == 'change_parameter':
                if value is None and direction == 'set':
                    return self._result('error', 'Нужно целое целевое значение параметра.')
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10000):
                    return self._result('error', 'Нужно неотрицательное целое значение параметра или шага.')
            if goal in {'inspect_parameter', 'change_parameter'} and category not in _PARAMETERS:
                return self._result('clarification', 'Уточните конкретный параметр: чувствительность мыши, громкость музыки, разрешение или режим экрана.')
            # Separate permission result from an OS refusing otherwise-authorized input.
            try:
                self._guard(cancel, target_hwnd)
            except PermissionError as exc:
                return self._result('permission', str(exc))
            obs = self._observe(cancel, target_hwnd)
            for step in range(5):  # At most four observed clicks, followed by verification.
                tokens = obs['tokens']
                state = _state(tokens)
                data['observed_state'] = state
                if notify:
                    notify('Распознано окно игры; проверяю элементы меню.')
                if goal == 'open_settings' and state == 'settings':
                    if _category_visible(tokens, category):
                        return self._result('done', 'Настройки видны на экране.', data, True)
                destination = None
                parameter = _matches(tokens, category) if category in _PARAMETERS else []
                if len(parameter) > 1:
                    raise _Unsafe('Найдено несколько одинаковых параметров. Выберите нужный вручную.')
                if parameter and goal in {'inspect_parameter', 'change_parameter'}:
                    row = self._row(tokens, parameter[0])
                    if category in {'resolution', 'display_mode'}:
                        data['preview'] = {'parameter': category, 'visible_values': [t['text'] for t in row], 'requested': value}
                        return self._result('clarification', 'Проверьте режим экрана вручную. Подтверждение сохранения изменений остаётся в игре.', data)
                    numbers = [t for t in row if re.fullmatch(r'\d+(?:[.,]\d+)?%?', t['text'].strip())]
                    if len(numbers) != 1:
                        raise _Unsafe('Текущее значение параметра нельзя определить однозначно.')
                    current = float(numbers[0]['text'].strip().rstrip('%').replace(',', '.'))
                    data['current_value'] = current
                    if goal == 'change_parameter' and 'target_value' not in data:
                        if direction in {'up', 'down'}:
                            target_value = current + (10 if value is None else value) * (1 if direction == 'up' else -1)
                            if category == 'music_volume':
                                target_value = min(100, max(0, target_value))
                        data['target_value'] = target_value
                    if goal == 'inspect_parameter' or current == target_value:
                        return self._result('done', f'Текущее значение: {current:g}.', data, True)
                    if 'previous_value' in data:
                        previous = data['previous_value']
                        if current == previous or (target_value-current)*(target_value-previous) < 0 or abs(target_value-current) >= abs(target_value-previous):
                            raise _Unsafe('Изменение не подтверждено или шаг не подходит. Продолжите вручную.')
                    sign = '+' if target_value > current else '-'
                    controls = [t for t in row if _norm(t['text']).replace('−','-') == sign]
                    if len(controls) != 1:
                        raise _Unsafe('Нет однозначной кнопки изменения параметра. Измените его вручную.')
                    control = controls[0]
                    data['previous_value'] = current
                elif goal == 'back':
                    if state not in {'settings','pause','menu'}:
                        raise _Unsafe('Меню не распознано. Откройте его вручную.')
                    control = self._unique(tokens, 'back')
                else:
                    label = 'settings'
                    if state == 'settings':
                        label = category if category in {'graphics','audio','controls','general'} else ('controls' if category == 'mouse_sensitivity' else 'audio' if category == 'music_volume' else 'graphics')
                    window = self._guard(cancel, target_hwnd, obs)
                    identity = (str(window.get('exe', '')) + ' ' + str(window.get('title', ''))).casefold()
                    if label == 'settings' and not _matches(tokens, 'settings') and 'geoguessr' in identity:
                        raise _Unsafe('Не нашла настройки на текущем экране GeoGuessr. Войдите в игру, затем повторите «Открой настройки игры».')
                    control = self._unique(tokens, label)
                    destination = label
                if step == 4:
                    raise _Unsafe('Достигнут предел четырёх действий. Продолжите вручную.')
                before = {_norm(t['text']) for t in tokens}
                self._click(control, obs, cancel, target_hwnd)
                # Cancellation-aware UI settling, followed by a fresh observation.
                if cancel.wait(.15):
                    raise _Stopped()
                obs = self._observe(cancel, target_hwnd)
                after = {_norm(t['text']) for t in obs['tokens']}
                if destination == 'settings' and _state(obs['tokens']) != 'settings':
                    raise _Unsafe('Открытие настроек не подтверждено. Продолжите вручную.')
                if destination in {'graphics', 'audio', 'controls'} and not _category_visible(obs['tokens'], destination):
                    raise _Unsafe('Открытие раздела не подтверждено. Продолжите вручную.')
                if goal == 'back':
                    if before != after and _state(obs['tokens']) in {'menu','settings','pause'}:
                        return self._result('done', 'Переход назад подтверждён по экрану.', data, True)
                    raise _Unsafe('Переход назад не удалось подтвердить. Продолжите вручную.')
                if before == after:
                    raise _Unsafe('Изменение экрана не подтверждено. Продолжите вручную.')
            raise _Unsafe('Продолжите настройку вручную.')
        except _Stopped:
            return self._result('cancelled', 'Управление игрой остановлено.', data)
        except (_Unsafe, PermissionError) as exc:
            return self._result('clarification', str(exc), data)
        except (ImportError, OSError, RuntimeError) as exc:
            return self._result('clarification', 'Локальное наблюдение или обычный ввод недоступны. Откройте меню вручную.', {**data, 'reason': type(exc).__name__})
        except Exception as exc:
            return self._result('error', 'Не удалось безопасно обработать меню игры.', {**data, 'reason': type(exc).__name__})
        finally:
            try:
                self.inputter.release_all()
            except Exception:
                pass

    @staticmethod
    def _unique(tokens, label):
        matches = _matches(tokens, label)
        if len(matches) != 1:
            raise _Unsafe('Нужный элемент меню не распознан однозначно. Откройте его вручную.')
        return matches[0]
