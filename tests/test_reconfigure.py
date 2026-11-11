from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import QCoreApplication
from alice_voice.config import Settings
from alice_voice.controller import VoiceSessionController

APP = QCoreApplication.instance() or QCoreApplication([])


class ReconfigureTests(unittest.TestCase):
    def test_appearance_change_preserves_running_response(self):
        runtime = Mock()
        self.c.runtime = runtime
        token = self.c.guard.begin('appearance')
        self.c.reconfigure(replace(self.c.settings, reduced_motion=True))
        runtime.cancel.assert_not_called()
        runtime.shutdown.assert_not_called()
        self.assertTrue(self.c.guard.valid(token))
        self.assertFalse(self.c.reconfiguring)

    def setUp(self):
        self.c = VoiceSessionController(Settings())

    def tearDown(self):
        self.c.capture = self.c.runtime = None
        self.c.shutdown()
        self.c._poll()

    def test_credential_change_retires_runtime_even_without_settings_change(self):
        runtime = Mock()
        runtime.is_alive.return_value = True
        self.c.runtime = runtime
        token = self.c.guard.begin('old-key')
        self.c.reconfigure(self.c.settings, credentials_changed=True)
        runtime.cancel.assert_called_once()
        runtime.shutdown.assert_called_once()
        self.assertFalse(self.c.guard.valid(token))
        self.assertTrue(self.c.reconfiguring)
        runtime.is_alive.return_value = False
        self.c._poll()
        self.assertFalse(self.c.reconfiguring)

    def test_active_workers_finish_before_new_settings_and_resume(self):
        capture = Mock()
        capture.is_alive.return_value = True
        capture.stop_event.is_set.return_value = False
        runtime = Mock()
        runtime.is_alive.return_value = True
        self.c.capture, self.c.runtime, self.c.mic_state = capture, runtime, 'listening'
        token = self.c.guard.begin('old')
        settings = replace(self.c.settings, ollama_model='new-model')
        self.c.reconfigure(settings)
        capture.stop.assert_called_once()
        runtime.shutdown.assert_called_once()
        self.assertFalse(self.c.guard.valid(token))
        self.assertNotEqual(self.c.settings.ollama_model, 'new-model')
        with patch.object(self.c, 'enable_microphone') as enable:
            self.c._poll()
            enable.assert_not_called()
            capture.is_alive.return_value = runtime.is_alive.return_value = False
            self.c._poll()
            self.assertEqual(self.c.settings.ollama_model, 'new-model')
            enable.assert_called_once()
        self.assertFalse(self.c.reconfiguring)

    def test_latest_request_wins_and_off_microphone_stays_off(self):
        self.c.reconfigure(replace(self.c.settings, provider='gemini'))
        self.c.reconfigure(replace(self.c.settings, ollama_model='latest'))
        with patch.object(self.c, 'enable_microphone') as enable:
            self.c._poll()
        enable.assert_not_called()
        self.assertEqual(self.c.settings.ollama_model, 'latest')

    def test_close_during_reconfiguration_never_reopens_microphone(self):
        self.c.capture = Mock()
        self.c.capture.is_alive.return_value = True
        self.c.capture.stop_event.is_set.return_value = False
        self.c.mic_state = 'listening'
        self.c.reconfigure(replace(self.c.settings, synthesizer='edge'))
        self.c.shutdown()
        self.c.capture.is_alive.return_value = False
        with patch.object(self.c, 'enable_microphone') as enable:
            self.c._poll()
        enable.assert_not_called()
