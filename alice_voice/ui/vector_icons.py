"""Consistent 24px outline glyphs rendered with Qt SVG (no font dependency)."""
from PyQt6.QtCore import QByteArray
from PyQt6.QtSvg import QSvgRenderer

PATHS = {
    'voice': '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8"/>',
    'mic': '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3"/>',
    'mic-off': '<path d="M9 5V4a3 3 0 0 1 6 0v7M9 9v3a3 3 0 0 0 5 2M5 10v2a7 7 0 0 0 12 5M19 10v2M12 19v3M2 2l20 20"/>',
    'dialog': '<path d="M21 11a8 8 0 0 1-8 8H7l-5 3 2-6a8 8 0 0 1-1-5 8 8 0 0 1 8-8h2a8 8 0 0 1 8 8Z"/>',
    'work': '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    'mini': '<rect x="2" y="3" width="20" height="18" rx="2"/><path d="M2 8h20"/><rect x="12" y="12" width="7" height="6" rx="1"/>',
    'settings': '<path d="M9 3 10 1h4l1 2 2 1 2-.2 2 3-1 2v3l1 2-2 3-2-.2-2 1-1 2h-4l-1-2-2-1-2 .2-2-3 1-2V9L3 7l2-3L7 4Z" transform="translate(0 1)"/><circle cx="12" cy="12" r="3.5"/>',
    'sidebar': '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16M5.5 7h1M5.5 10h1M5.5 13h1"/>',
    'speaker': '<path d="M11 4 6 8H2v8h4l5 4ZM15 8a6 6 0 0 1 0 8M18 5a10 10 0 0 1 0 14"/>',
    'camera': '<rect x="2" y="6" width="13" height="12" rx="2"/><path d="m15 10 7-4v12l-7-4"/>',
    'camera-off': '<path d="M3 6h9a2 2 0 0 1 2 2v6M14 10l8-4v12l-4-2M2 10v6a2 2 0 0 0 2 2h8M2 2l20 20"/>',
    'more': '<circle cx="4" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="20" cy="12" r="1"/>',
}

def renderer(name, color, parent):
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{PATHS[name]}</svg>'
    return QSvgRenderer(QByteArray(svg.encode()), parent)
