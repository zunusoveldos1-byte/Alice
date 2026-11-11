"""Public UI imports kept compatible with the original alice_voice.ui module."""
from .dialogs import MetricsDialog, SettingsDialog, MicrophoneDiscovery
from .main_window import MainWindow

VoiceWindow = MainWindow

__all__ = ['MainWindow', 'VoiceWindow', 'SettingsDialog', 'MetricsDialog']
