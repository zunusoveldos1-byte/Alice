from threading import Event
from time import perf_counter
from unittest import TestCase
from unittest.mock import Mock
from PyQt6.QtCore import QCoreApplication
from alice_voice.config import Settings
from alice_voice.controller import VoiceSessionController

APP=QCoreApplication.instance() or QCoreApplication([])


class ControlSafety(TestCase):
    def test_own_playback_cannot_confirm_power_even_with_headphones(self):
        c=VoiceSessionController(Settings(owner_only=True,headphones=True));c.timer.stop()
        c.mic_state='listening';c.power_pending=True;c.runtime=Mock()
        try:
            now=perf_counter();c.echo.start(now-.2)
            c._phrase(0,{'id':'echo','text':'Да','start':now-.1,'speech_end':now,'ready':now+.1,'owner_verified':True})
            c.runtime.submit.assert_not_called()
        finally:
            c.runtime=None;c.shutdown();c._poll()

    def test_emergency_stop_only_cancels_control_never_authorizes_actions(self):
        c=VoiceSessionController(Settings(owner_only=True));c.timer.stop();c.stop_actions=Mock()
        try:
            c._safety_stop(0);c.stop_actions.assert_not_called()
            c.power_pending=True;c._safety_stop(1);c.stop_actions.assert_not_called()
            c._safety_stop(0);c.stop_actions.assert_called_once()
        finally:
            c.shutdown();c._poll()

    def test_legacy_disabled_windows_permission_survives_settings_migration(self):
        settings=Settings.from_dict({'desktop_commands':False})
        self.assertFalse(settings.permission_windows)
        self.assertFalse(settings.permission_window_control)
        self.assertFalse(settings.permission_browser)
