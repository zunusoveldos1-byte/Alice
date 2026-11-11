import unittest
from tests import test_credential_ui as credential_ui


class LMStudioUITests(unittest.TestCase):
    run_ui = credential_ui.CredentialUITests.run_ui
    def test_lmstudio_selection_models_and_token_actions(self):
        self.run_ui('''
            from unittest.mock import patch
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.ui.dialogs import SettingsDialog
            app = QApplication([])
            with patch('alice_voice.ui.dialogs.MicrophoneDiscovery.start'), \\
                 patch('alice_voice.secrets.save_lmstudio_token') as save, \\
                 patch('alice_voice.secrets.delete_lmstudio_token') as delete:
                dialog = SettingsDialog(Settings())
                assert dialog.inputs['provider'].findData('lmstudio') >= 0
                assert dialog.inputs['lmstudio_url'].text() == 'http://localhost:1234/v1'
                assert dialog.values().provider == 'ollama'
                dialog.inputs['provider'].setCurrentIndex(dialog.inputs['provider'].findData('lmstudio'))
                dialog.inputs['lmstudio_model'].setCurrentText('real/id')
                assert dialog.values().lmstudio_model == 'real/id'
                assert dialog.values().provider == 'lmstudio'
                dialog.lmstudio_token.setText('fixture-token')
                dialog.save_credentials()
                save.assert_called_once_with('fixture-token')
                assert dialog.lmstudio_token.text() == ''
                assert 'fixture-token' not in repr(dialog.values())
                dialog.delete_lmstudio.setChecked(True)
                dialog.save_credentials()
                delete.assert_called_once_with()
                dialog.close()
        ''')

    def test_check_uses_actual_ids_and_drops_stale_results(self):
        self.run_ui('''
            from unittest.mock import patch
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.ui.dialogs import SettingsDialog
            app = QApplication([])
            with patch('alice_voice.ui.dialogs.MicrophoneDiscovery.start'):
                d = SettingsDialog(Settings(lmstudio_model='previous/id'))
                d._lm_check_serial = 1
                d._lm_check_fingerprint = d._lm_fingerprint()
                d._lmstudio_result(1, {'ids': ['actual/id'], 'tools': 'not verified'}, '')
                assert d.inputs['lmstudio_model'].itemText(0) == 'actual/id'
                assert d.inputs['lmstudio_model'].currentText() == 'previous/id'
                d._lm_check_fingerprint = d._lm_fingerprint()
                d.inputs['lmstudio_model'].setCurrentText('new/id')
                d._lmstudio_result(1, {'ids': ['old/id'], 'tools': 'old result'}, '')
                assert d.inputs['lmstudio_model'].currentText() == 'new/id'
                assert 'изменены' in d.lmstudio_notice.text()
                d.show()
                app.processEvents()
                assert d.height() < 1000, d.height()
                d.close()
        ''')
