"""Ollama preset selection in real Qt widgets, without a running server."""
import unittest

from tests import test_main_window as main_window_tests


class OllamaUITests(unittest.TestCase):
    run_ui = main_window_tests.MainWindowTests.run_ui

    def test_installed_models_refresh_keeps_selection_and_handles_errors(self):
        self.run_ui('''
            from unittest.mock import patch
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.ui.dialogs import SettingsDialog
            app = QApplication([])
            with patch('alice_voice.ui.dialogs.MicrophoneDiscovery.start'):
                dialog = SettingsDialog(Settings(ollama_model='qwen3:1.7b'))
                dialog._ollama_serial = 1
                dialog._ollama_url = dialog.inputs['ollama_url'].text().strip()
                dialog._ollama_result(1, ['qwen3.5:4b', 'qwen3:1.7b', 'my-model:latest'], '')
                picker = dialog.inputs['ollama_model']
                assert picker.currentText() == 'qwen3:1.7b'
                assert picker.findText('my-model:latest') >= 0
                assert picker.count() == 3
                dialog._ollama_result(0, ['stale-model'], '')
                assert picker.findText('stale-model') == -1
                dialog._ollama_result(1, [], 'Ollama unavailable')
                assert picker.currentText() == 'qwen3:1.7b'
                assert picker.findText('my-model:latest') >= 0
                assert dialog.refresh_ollama.isEnabled()
                dialog.inputs['ollama_url'].setText('http://localhost:11435')
                dialog._ollama_result(1, ['wrong-server-model'], '')
                assert picker.findText('wrong-server-model') == -1
                dialog.close()
        ''')

    def test_refresh_fetches_ollama_tags_without_changing_model(self):
        self.run_ui('''
            from unittest.mock import patch
            import httpx
            from PyQt6.QtTest import QTest
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.ui.dialogs import SettingsDialog
            app = QApplication([])
            response = httpx.Response(200, json={'models': [
                {'name': 'qwen3.5:4b'}, {'name': 'qwen3:1.7b'}, {'name': 'custom:latest'}
            ]}, request=httpx.Request('GET', 'http://127.0.0.1:11434/api/tags'))
            with patch('alice_voice.ui.dialogs.MicrophoneDiscovery.start'), \\
                 patch('httpx.Client.get', return_value=response) as get:
                dialog = SettingsDialog(Settings(ollama_model='qwen3:1.7b'))
                dialog.refresh_ollama.click()
                for _ in range(100):
                    QTest.qWait(10)
                    if dialog.inputs['ollama_model'].findText('custom:latest') >= 0:
                        break
                assert dialog.inputs['ollama_model'].findText('custom:latest') >= 0
                assert dialog.values().ollama_model == 'qwen3:1.7b'
                assert get.call_args.args[0] == 'http://127.0.0.1:11434/api/tags'
                dialog.close()
        ''')

    def test_settings_presets_custom_models_and_save_roundtrip(self):
        self.run_ui('''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from unittest.mock import patch
            from PyQt6.QtWidgets import QApplication, QComboBox, QWidget
            from alice_voice.config import Settings
            from alice_voice.ui.dialogs import SettingsDialog
            app = QApplication([])
            parent = QWidget()
            parent._toggle_camera = lambda: None
            with TemporaryDirectory() as directory, patch('alice_voice.ui.dialogs.MicrophoneDiscovery.start'):
                path = Path(directory) / 'settings.json'
                for embedded in (False, True):
                    dialog = SettingsDialog(Settings(ollama_model='my-model:custom'), parent, embedded=embedded)
                    model = dialog.inputs['ollama_model']
                    assert isinstance(model, QComboBox) and model.isEditable()
                    assert model.currentText() == 'my-model:custom'
                    for preset in ('qwen3.5:4b', 'qwen3:1.7b'):
                        index = model.findText(preset)
                        assert index >= 0, preset
                        model.setCurrentIndex(index)
                        dialog.values().save(path)
                        saved = Settings.load(path)
                        assert saved.ollama_model == preset
                        reopened = SettingsDialog(saved, parent, embedded=embedded)
                        assert reopened.inputs['ollama_model'].currentText() == preset
                        reopened.close()
                    model.setEditText('  another-model:custom  ')
                    assert dialog.values().ollama_model == 'another-model:custom'
                    model.setEditText('   ')
                    try:
                        dialog.values()
                    except ValueError:
                        pass
                    else:
                        raise AssertionError('Blank model must be rejected')
                    dialog.close()
        ''')

    def test_header_presets_save_and_stay_scoped_to_ollama(self):
        self.run_ui('''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from PyQt6.QtTest import QTest
            from PyQt6.QtWidgets import QApplication
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.main_window import MainWindow
            app = QApplication([])
            with TemporaryDirectory() as directory:
                path = Path(directory) / 'settings.json'
                controller = VoiceSessionController(Settings(ollama_model='my-model:custom'))
                window = MainWindow(controller, path)
                assert window.model_combo.currentText() == 'my-model:custom'
                for preset in ('qwen3.5:4b', 'qwen3:1.7b'):
                    index = window.model_combo.findText(preset)
                    assert index >= 0, preset
                    window.model_combo.setCurrentIndex(index)
                    window.model_combo.activated.emit(index)
                    for _ in range(100):
                        QTest.qWait(10)
                        if path.exists() and Settings.load(path).ollama_model == preset:
                            break
                    assert Settings.load(path).ollama_model == preset
                index = window.provider_combo.findData('gemini')
                window.provider_combo.setCurrentIndex(index)
                window.provider_combo.activated.emit(index)
                QTest.qWait(150)
                assert window.model_combo.findText('qwen3.5:4b') == -1
                assert window.model_combo.findText('qwen3:1.7b') == -1
                window.close()
                QTest.qWait(150)
        ''')
