"""Context controls remain visible and cancellable in the real Qt shell."""
import unittest

from tests import test_main_window


class ContextUiTests(unittest.TestCase):
    run_ui = test_main_window.MainWindowTests.run_ui

    def test_permission_choices_round_trip_independently(self):
        self.run_ui('''
            from unittest.mock import patch
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.ui.dialogs import SettingsDialog
            app = QApplication([])
            with patch.object(SettingsDialog, '_refresh_devices'):
                dialog = SettingsDialog(Settings())
            permissions = ('permission_windows', 'permission_window_control',
                           'permission_browser', 'permission_screen', 'permission_input',
                           'permission_games', 'permission_power')
            assert all(key in dialog.inputs for key in permissions), 'separate permissions missing'
            dialog.show()
            for key in permissions:
                dialog.inputs[key].setChecked(key in ('permission_browser', 'permission_games'))
            dialog.inputs['natural_tools'].setChecked(False)
            saved = dialog.values()
            assert saved.permission_browser and saved.permission_games
            assert not saved.permission_windows and not saved.permission_window_control
            assert not saved.permission_screen and not saved.permission_input and not saved.permission_power
            assert not saved.natural_tools
            assert dialog.inputs['desktop_commands'].isHidden()
            assert dialog.permission_note.text() and dialog.permission_note.wordWrap()
        ''')

    def test_control_summary_power_confirmation_and_log_on_all_pages(self):
        self.run_ui('''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app = QApplication([])
            with TemporaryDirectory(dir='.') as directory:
                c = VoiceSessionController(Settings())
                w = MainWindow(c, Path(directory) / 'settings.json')
                w.show()
                c.control_summary = 'Для выключения скажите: «Подтверждаю выключение компьютера»'
                c.power_pending = True
                c.action_log = [dict(status='ok', tool='windows.launch', message='Открыт Блокнот', time='12:34')]
                w._render()
                assert hasattr(w, 'control_status'), 'global confirmation banner missing'
                assert w.control_status.text() == c.control_summary
                assert w.control_status.isVisible()
                assert w.global_stop_btn.isVisible() and w.global_stop_btn.isEnabled()
                for page in ('dialog', 'work', 'voice'):
                    w.select_page(page); w._render()
                    assert w.control_status.isVisible()
                w.select_page('work')
                assert w.work_control_status.text() == c.control_summary
                assert w.action_log.isReadOnly()
                assert 'Открыт Блокнот' in w.action_log.toPlainText()
                assert 'windows.launch' not in w.action_log.toPlainText()
                w.allow_close = True; w.close(); c.shutdown()
        ''')

    def test_stop_ready_without_response_and_typed_input(self):
        self.run_ui('''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app = QApplication([])
            with TemporaryDirectory(dir='.') as directory:
                c = VoiceSessionController(Settings())
                commands = []
                def stop():
                    c.automation_active = False
                    c.power_pending = False
                c.stop_actions = stop
                c.request_text = commands.append
                w = MainWindow(c, Path(directory) / 'settings.json')
                assert not w.stop_btn.isEnabled()
                c.automation_active = True
                w._render()
                assert w.stop_btn.isEnabled(), 'automation must be stoppable between responses'
                w.stop_btn.click(); w._render()
                assert not c.automation_active and not w.stop_btn.isEnabled()
                c.power_pending = True
                w._render()
                assert w.stop_btn.isEnabled()
                w.global_stop_btn.click(); w._render()
                assert not c.power_pending and not w.stop_btn.isEnabled()
                w.command_input.setText('  открой блокнот  ')
                w.command_input.returnPressed.emit()
                assert commands == ['открой блокнот']
                assert not w.command_input.text()
                w.command_input.returnPressed.emit()
                assert commands == ['открой блокнот']
                w.allow_close = True; w.close(); c.shutdown()
        ''')
