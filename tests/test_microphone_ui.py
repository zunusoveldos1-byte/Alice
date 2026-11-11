"""Exercise real Qt widgets in an isolated offscreen QApplication."""
import os
import subprocess
import sys
import textwrap
import unittest


class MicrophoneUITests(unittest.TestCase):
    def test_named_selection_refresh_and_missing_device(self):
        script = textwrap.dedent('''
            from unittest.mock import patch
            from PyQt6.QtWidgets import QApplication, QComboBox
            from alice_voice.audio import InputDevice
            from alice_voice.config import Settings
            from alice_voice.ui import SettingsDialog
            app = QApplication([])
            with patch('alice_voice.ui.MicrophoneDiscovery.start'):
                dialog = SettingsDialog(Settings(input_device=7))
                picker = dialog.inputs['input_device']
                assert isinstance(picker, QComboBox)
                assert picker.currentData() == 7
                dialog._devices_ready([InputDevice(7, 'USB Mic', 'WASAPI', True, True),
                                       InputDevice(9, 'Laptop Mic', 'MME', False, True)])
                assert 'USB Mic' in picker.currentText()
                assert dialog.values().input_device == 7
                picker.setCurrentIndex(picker.findData(9))
                dialog._devices_ready([InputDevice(9, 'Laptop Mic', 'MME', True, True)])
                assert dialog.values().input_device == 9
                dialog._devices_ready([])
                assert picker.currentData() == 9, 'must not silently switch devices'
                try:
                    dialog.values()
                except ValueError:
                    pass
                else:
                    raise AssertionError('unavailable device was accepted')
                picker.setCurrentIndex(picker.findData(None))
                assert dialog.values().input_device is None
                assert dialog.values().min_speech_ms == 96
                dialog.close()
        ''')
        result = subprocess.run([sys.executable, '-B', '-c', script],
                                env=dict(os.environ, QT_QPA_PLATFORM='offscreen'),
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
