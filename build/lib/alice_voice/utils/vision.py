"""Explicit, single-frame desktop capture. No image files or retained frames."""
from io import BytesIO
import math
import re

MAX_IMAGE_BYTES = 2 * 1024 * 1024
_SCREEN_COMMAND = re.compile(
    r'^\s*(?:посмотри\s+на\s+экран|посмотри\s+что\s+на\s+экране|'
    r'что\s+на\s+экране|проанализируй\s+экран)(?!\w)', re.IGNORECASE)


def is_screen_request(text: str) -> bool:
    return bool(_SCREEN_COMMAND.match(text))


def list_capture_monitors() -> list[dict]:
    """Return physical monitor bounds, excluding the combined virtual desktop."""
    try:
        import mss
        with mss.mss() as capture:
            return [dict(index=index, **{key: int(monitor[key]) for key in ('left', 'top', 'width', 'height')},
                         is_primary=bool(monitor.get('is_primary', index == 1)))
                    for index, monitor in enumerate(capture.monitors[1:], 1)]
    except Exception:
        raise RuntimeError('Не удалось получить список мониторов.') from None


def active_window_region() -> dict:
    """Read visible foreground-window bounds in physical pixels on Windows."""
    try:
        import ctypes
        from ctypes import wintypes
        import win32gui
        window = win32gui.GetForegroundWindow()
        if not window or win32gui.IsIconic(window):
            raise ValueError('No visible foreground window')
        rect = wintypes.RECT()
        get_bounds = ctypes.windll.dwmapi.DwmGetWindowAttribute
        get_bounds.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        get_bounds.restype = ctypes.c_long
        if get_bounds(window, 9, ctypes.byref(rect), ctypes.sizeof(rect)) == 0:
            left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
        else:
            left, top, right, bottom = win32gui.GetWindowRect(window)
        monitors = list_capture_monitors()
        left = max(left, min(m['left'] for m in monitors))
        top = max(top, min(m['top'] for m in monitors))
        right = min(right, max(m['left'] + m['width'] for m in monitors))
        bottom = min(bottom, max(m['top'] + m['height'] for m in monitors))
        if right <= left or bottom <= top:
            raise ValueError('Window is outside the desktop')
        return dict(left=left, top=top, width=right - left, height=bottom - top)
    except Exception:
        raise RuntimeError('Активное окно недоступно. Выберите видимое окно и повторите снимок.') from None


def map_preview_region(display: tuple, selection: tuple, native: dict) -> dict:
    """Map a selection in a letterboxed preview to physical desktop pixels."""
    x, y, width, height = display
    sx, sy, sw, sh = selection
    if width <= 0 or height <= 0 or sw <= 0 or sh <= 0:
        raise ValueError('Выделите область внутри изображения.')
    left, top = max(x, sx), max(y, sy)
    right, bottom = min(x + width, sx + sw), min(y + height, sy + sh)
    if right <= left or bottom <= top:
        raise ValueError('Выделите область внутри изображения.')
    px = max(0, math.floor((left - x) * native['width'] / width))
    py = max(0, math.floor((top - y) * native['height'] / height))
    pr = min(native['width'], math.ceil((right - x) * native['width'] / width))
    pb = min(native['height'], math.ceil((bottom - y) * native['height'] / height))
    return dict(left=native['left'] + px, top=native['top'] + py, width=pr - px, height=pb - py)


def capture_screen_jpeg(max_width: int = 1280, quality: int = 75, *, region: dict | None = None) -> bytes:
    """Capture the primary monitor, fit within 1280px and encode JPEG in RAM.

    Create and close MSS on the calling worker thread. Portrait monitors are
    also height-limited to bound pixels and encoded size. Never upscale.
    """
    if not 1 <= max_width <= 1280 or not 1 <= quality <= 95:
        raise ValueError('Неверный размер или качество снимка экрана.')
    try:
        import mss
        from PIL import Image
        with mss.mss() as capture:
            monitor = capture.primary_monitor if region is None else {
                key: region[key] for key in ('left', 'top', 'width', 'height')}
            if (any(type(monitor[key]) is not int for key in ('left', 'top', 'width', 'height'))
                    or monitor['width'] <= 0 or monitor['height'] <= 0
                    or monitor['width'] * monitor['height'] > 32_000_000):
                raise ValueError('Screen too large')
            shot = capture.grab(monitor)
            with Image.frombytes('RGB', shot.size, shot.rgb) as image, BytesIO() as buffer:
                image.thumbnail((max_width, max_width), Image.Resampling.LANCZOS)
                image.save(buffer, format='JPEG', quality=quality, optimize=True)
                data = buffer.getvalue()
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError('Image too large')
        return data
    except Exception:
        raise RuntimeError('Не удалось захватить экран. Проверьте доступ к рабочему столу и установку mss/Pillow.') from None
