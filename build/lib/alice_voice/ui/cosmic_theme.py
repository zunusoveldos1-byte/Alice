"""Shared design tokens and Qt styling for the cosmic desktop UI."""
PALETTE = dict(background='#071426', sidebar='#0A1626', text='#EEF6FF', muted='#ABC1DD', accent='#32D7FF', background_mid='#0B2040', background_bottom='#143C65',
               control='#1B3654', control_border='#40668A', icon='#CEE3FA',
               orb_idle_opacity=1.0, glow_strength=.94)
STYLE = '''
QWidget { color: #EEF6FF; font-family: 'Segoe UI'; font-size: 13px; }
QLabel { background: transparent; }
QLabel#brand { font-size: 23px; font-weight: 600; }
QLabel#sectionTitle { font-size: 19px; font-weight: 600; }
QLabel#clock { font-size: 18px; font-weight: 600; color: #EEF6FF; background: #0A182B; border: 1px solid #142D48; border-radius: 12px; padding: 5px 12px; }
QLabel#stateLabel { font-size: 36px; font-weight: 600; }
QLabel#subtitle { font-size: 18px; color: #AECFF4; }
QLabel#muted, QLabel#userText { color: #ABC1DD; }
QLabel#userText { font-size: 14px; }
QPushButton { background: transparent; border: 1px solid transparent; border-radius: 10px; padding: 7px; }
QPushButton:hover { background: #315477; }
QWidget { outline: none; }
QPushButton:pressed { background: #214365; }
QPushButton[keyboardFocus="true"]:focus, QComboBox[keyboardFocus="true"]:focus,
QLineEdit[keyboardFocus="true"]:focus, QSpinBox[keyboardFocus="true"]:focus,
QDoubleSpinBox[keyboardFocus="true"]:focus, QPlainTextEdit[keyboardFocus="true"]:focus,
QCheckBox[keyboardFocus="true"]:focus, QSlider[keyboardFocus="true"]:focus {
    border: 1px solid #51B5FF;
}
QPushButton:disabled { color: #65788f; }
QPushButton[nav="true"] { text-align: left; padding: 8px 12px; border-radius: 6px; font-size: 15px; }
QPushButton[nav="true"]:checked { background: #10263F; border-left: 2px solid #32d7ff; }
QPushButton[segment="true"]:checked { background: #104775; border: 1px solid #238cff; color: #32d7ff; }
QFrame#segments { border: 1px solid #315477; border-radius: 18px; }
QFrame#dock, QFrame#volumePopup { background: #0C1D33; border: 1px solid #3B6084; border-radius: 24px; }
QFrame#volumePopup { border-radius: 12px; }
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox { background: #142A43; border: 1px solid #345679; border-radius: 7px; padding: 7px; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView { background: #142A43; selection-background-color: #254A6A; }
QPlainTextEdit { background: #0D2035; border: 1px solid #2D4967; border-radius: 12px; padding: 16px; font-size: 15px; }
QDialog, QMenu { background: #10253D; }
QMenu { border: 1px solid #3B6084; padding: 7px; }
QMenu::item { padding: 9px 20px; border-radius: 5px; }
QMenu::item:selected { background: #20496A; }
QTabWidget::pane { border: 1px solid #3B6084; }
QTabBar::tab { padding: 9px; background: #193653; }
QSlider::groove:horizontal { background: #426586; height: 4px; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #32d7ff; }
QSlider::handle:horizontal { background: #a4f2ff; width: 12px; margin: -5px 0; border-radius: 6px; }
QScrollArea, QWidget#scrollContent { background: transparent; border: none; }
QListWidget { background: #09172A; border: none; border-radius: 12px; padding: 5px; }
QListWidget::item { padding: 12px 8px; border-radius: 6px; }
QListWidget::item:selected { background: #17314D; color: #EEF6FF; }
QWidget#settingsSection { background: #08172A; }
QScrollBar:vertical { background: #0A182B; width: 8px; margin: 0; }
QScrollBar::handle:vertical { background: #315477; min-height: 28px; border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
'''
SIDEBAR_WIDTH = 290
COMPACT_SIDEBAR_WIDTH = 76
FRAME_INTERVAL_MS = 50
VOLUME_HIDE_DELAY_MS = 250
VOLUME_FADE_MS = 130
ORB_MAX_SIZE = 340
