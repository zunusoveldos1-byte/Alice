"""Real settings widgets, fake credential actions; no actual user keys."""
import os
import subprocess
import sys
import textwrap
import unittest


class CredentialUITests(unittest.TestCase):
    def run_ui(self, script):
        result = subprocess.run([sys.executable, '-B', '-c', textwrap.dedent(script)],
                                env=dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONIOENCODING='utf-8'),
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_replace_delete_and_unchanged_fields_never_enter_settings(self):
        self.run_ui('''
            from unittest.mock import patch
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.ui.dialogs import SettingsDialog
            app = QApplication([])
            with patch('alice_voice.ui.dialogs.MicrophoneDiscovery.start'), \
                 patch('alice_voice.secrets.save_elevenlabs_key') as save, \
                 patch('alice_voice.secrets.delete_elevenlabs_key') as delete:
                dialog = SettingsDialog(Settings())
                assert dialog.elevenlabs_key.text() == ''
                assert 'elevenlabs_api_key' not in dialog.values().to_dict()
                dialog.save_credentials()
                save.assert_not_called()
                delete.assert_not_called()
                dialog.elevenlabs_key.setText('fixture-ui-value')
                dialog.save_credentials()
                save.assert_called_once_with('fixture-ui-value')
                assert dialog.elevenlabs_key.text() == ''
                assert dialog.credentials_changed
                dialog.delete_elevenlabs.setChecked(True)
                dialog.save_credentials()
                delete.assert_called_once_with()
                assert not dialog.delete_elevenlabs.isChecked()
                assert 'fixture-ui-value' not in repr(dialog.values())
                dialog.close()
        ''')

    def test_failure_retains_input_for_retry_and_rejects_ambiguous_delete(self):
        self.run_ui('''
            from unittest.mock import patch
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.secrets import CredentialError
            from alice_voice.ui.dialogs import SettingsDialog
            app = QApplication([])
            with patch('alice_voice.ui.dialogs.MicrophoneDiscovery.start'), \
                 patch('alice_voice.secrets.save_elevenlabs_key', side_effect=CredentialError('Хранилище недоступно')) as save:
                dialog = SettingsDialog(Settings())
                dialog.elevenlabs_key.setText('fixture-ui-value')
                try: dialog.save_credentials()
                except CredentialError: pass
                else: raise AssertionError('failure was hidden')
                assert dialog.elevenlabs_key.text() == 'fixture-ui-value'
                assert 'fixture-ui-value' not in repr(dialog.values())
                dialog.delete_elevenlabs.setChecked(True)
                try: dialog.save_credentials()
                except ValueError: pass
                else: raise AssertionError('conflicting actions were accepted')
                assert save.call_count == 1
                dialog.close()
        ''')

    def test_main_window_reports_error_without_persisting_or_clearing_input(self):
        self.run_ui('''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from unittest.mock import patch
            from PyQt6.QtWidgets import QApplication, QDialog
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.secrets import CredentialError
            from alice_voice.ui.dialogs import SettingsDialog
            from alice_voice.ui.main_window import MainWindow
            app = QApplication([])
            with TemporaryDirectory() as directory, patch('alice_voice.ui.dialogs.MicrophoneDiscovery.start'):
                path = Path(directory) / 'settings.json'
                Settings().save(path)
                before = path.read_bytes()
                c = VoiceSessionController(Settings())
                w = MainWindow(c, path)
                w._show_settings()
                dialog = w.settings_page
                dialog.elevenlabs_key.setText('fixture-ui-value')
                with patch.object(c, 'reconfigure', wraps=c.reconfigure) as refresh, \
                     patch('alice_voice.secrets.save_elevenlabs_key', side_effect=CredentialError('Хранилище недоступно')):
                    w._save_settings_page()
                    assert refresh.call_args.kwargs == {'credentials_changed': True}
                assert path.read_bytes() == before
                assert 'Хранилище недоступно' in dialog.error.text()
                assert dialog.elevenlabs_key.text() == 'fixture-ui-value'
                w._discard_settings_page()
                assert dialog.elevenlabs_key.text() == ''
                w.close()
                app.processEvents()
        ''')
