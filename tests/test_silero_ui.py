"""Qt Widgets live in their own process; no models/network needed for settings."""
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


class SileroSettingsUI(unittest.TestCase):
    def test_silero_settings_and_loading_states_do_not_load_torch_on_gui_thread(self):
        script = textwrap.dedent('''
            import sys
            from unittest.mock import patch
            from PyQt6.QtWidgets import QApplication, QTabWidget
            from alice_voice.config import Settings
            from alice_voice.controller import VoiceSessionController
            from alice_voice.ui.dialogs import SettingsDialog
            from alice_voice.ui.main_window import MainWindow
            from tempfile import TemporaryDirectory
            from pathlib import Path
            app = QApplication([])
            with patch('alice_voice.ui.dialogs.MicrophoneDiscovery.start'):
                for engine in ('sapi', 'edge', 'elevenlabs', 'silero'):
                    dialog = SettingsDialog(Settings(synthesizer=engine))
                    assert dialog.values().synthesizer == engine
                    assert dialog.values().silero_voice == 'xenia'
                    dialog.inputs['silero_voice'].setCurrentText('baya')
                    assert dialog.values().silero_voice == 'baya'
                    if engine == 'silero':
                        dialog.findChild(QTabWidget).setCurrentIndex(2)
                        dialog.show(); app.processEvents()
                        dialog.grab().save('.build-tmp/silero-settings.png')
                    dialog.close()
            with TemporaryDirectory() as directory:
                c = VoiceSessionController(Settings(synthesizer='elevenlabs'))
                window = MainWindow(c, Path(directory) / 'settings.json')
                assert window.voice_combo.findData('silero') >= 0
                assert window.voice_combo.currentData() == 'elevenlabs'
                for stage in ('silero_download', 'silero_loading'):
                    c.stage = stage
                    assert window._mode()[0] == 'loading'
                c.stage = 'silero_ready'
                assert window._mode()[0] != 'loading'
                assert 'Загружаю Silero' not in window._mode()[1]
                assert 'torch' not in sys.modules
                window.close(); c.shutdown()
        ''')
        result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
