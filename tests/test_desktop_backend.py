import unittest
from threading import Event
from unittest.mock import Mock, patch

from alice_voice.desktop_backend import DesktopBackend


class DesktopBackendTests(unittest.TestCase):
    def setUp(self):
        self.backend = DesktopBackend(None)
        self.cancel = Event()

    def test_cancel_prevents_every_mutation(self):
        self.cancel.set()
        with patch('alice_voice.desktop_backend.subprocess.Popen') as launch:
            result = self.backend.execute('power', {'action': 'shutdown'}, self.cancel)
        self.assertEqual(result['status'], 'cancelled')
        launch.assert_not_called()

    def test_relative_volume_uses_requested_amount(self):
        endpoint = Mock()
        endpoint.level.side_effect = [40, 65, 65]
        endpoint.muted.return_value = False
        with patch('alice_voice.desktop_backend.Endpoint') as cls:
            cls.return_value.__enter__.return_value = endpoint
            result = self.backend.execute('volume', {'mode': 'up', 'value': 25}, self.cancel)
        endpoint.set_level.assert_called_once_with(65)
        self.assertTrue(result['verified'])

    def test_ambiguous_apps_offer_choices_and_do_not_launch(self):
        self.backend._catalog = {'a': {'name': 'Game One', 'id': 'a', 'path': 'one.exe'},
                                 'b': {'name': 'Game Two', 'id': 'b', 'path': 'two.exe'}}
        with patch('alice_voice.desktop_backend.os.startfile') as launch:
            result = self.backend.execute('launch_app', {'name': 'Game'}, self.cancel)
        self.assertEqual(result['status'], 'clarification')
        self.assertEqual(len(result['data']['choices']), 2)
        launch.assert_not_called()

    def test_window_change_prevents_action(self):
        with patch.object(self.backend, 'active_window', return_value={'hwnd': 20}), \
             patch.object(self.backend, '_window_mutation') as mutate:
            result = self.backend.execute('window_control', {'mode': 'close', 'name': ''}, self.cancel, target_hwnd=10)
        self.assertEqual(result['status'], 'clarification')
        mutate.assert_not_called()

    def test_ambiguous_windows_are_not_first_match(self):
        windows = [{'hwnd': 1, 'title': 'Game one', 'exe': 'one.exe'},
                   {'hwnd': 2, 'title': 'Game two', 'exe': 'two.exe'}]
        with patch.object(self.backend, 'list_windows', return_value=windows), \
             patch.object(self.backend, '_window_mutation') as mutate:
            result = self.backend.execute('window_control', {'mode': 'close', 'name': 'Game'}, self.cancel)
        self.assertEqual(result['status'], 'clarification')
        mutate.assert_not_called()

    def test_stable_window_choice_and_disappeared_window(self):
        windows = [{'hwnd': 31, 'title': 'X'}, {'hwnd': 32, 'title': 'X'}]
        with patch.object(self.backend, 'list_windows', return_value=windows), \
             patch.object(self.backend, '_window_mutation', return_value={'status':'done'}) as mutate:
            self.backend.execute('window_control', {'mode':'close','window_id':32}, self.cancel)
            self.assertEqual(mutate.call_args.args[0],32)
            mutate.reset_mock()
            result=self.backend.execute('window_control', {'mode':'close','window_id':33}, self.cancel)
            self.assertEqual(result['status'],'cancelled')
            mutate.assert_not_called()

    def test_unknown_application_cannot_be_path_or_uri(self):
        self.backend._catalog = {}
        result = self.backend.execute('launch_app', {'name': 'cmd.exe /c calc'}, self.cancel)
        self.assertEqual(result['status'], 'clarification')

    def test_power_uses_fixed_executable_without_force(self):
        with patch('alice_voice.desktop_backend.subprocess.Popen') as launch:
            result = self.backend.execute('power', {'action': 'restart'}, self.cancel)
        command = launch.call_args.args[0]
        self.assertEqual(command[1:], ['/r', '/t', '0'])
        self.assertTrue(command[0].lower().endswith('system32\\shutdown.exe'))
        self.assertFalse(result['verified'])

    def test_catalog_excludes_interpreters(self):
        for name in ['cmd.exe', 'powershell.exe', 'pwsh.exe', 'python.exe', 'wscript.exe', 'regedit.exe', 'wt.exe']:
            self.assertFalse(self.backend._safe_executable(name))
        self.assertTrue(self.backend._safe_executable('firefox.exe'))

    def test_invalid_page_and_mode_fail_closed(self):
        for name, args in [('windows_settings', {'page': 'arbitrary:uri'}),
                           ('power', {'action': 'anything'}),
                           ('media', {'command': 'arbitrary'})]:
            self.assertEqual(self.backend.execute(name, args, self.cancel)['status'], 'error')

    def test_cancel_during_volume_read_prevents_write(self):
        endpoint = Mock()
        def level():
            self.cancel.set()
            return 40
        endpoint.level.side_effect = level
        with patch('alice_voice.desktop_backend.Endpoint') as cls:
            cls.return_value.__enter__.return_value = endpoint
            result = self.backend.execute('volume', {'mode': 'up', 'value': 20}, self.cancel)
        self.assertEqual(result['status'], 'cancelled')
        endpoint.set_level.assert_not_called()

    def test_close_posts_wm_close_without_terminating(self):
        gui = Mock()
        gui.IsWindow.return_value = True
        with patch.dict('sys.modules', {'win32gui': gui}):
            result = self.backend._window_mutation(42, 'close', self.cancel)
        gui.PostMessage.assert_called_once_with(42, 0x0010, 0, 0)
        self.assertFalse(result['verified'])

    def test_target_rechecked_inside_native_window_mutation(self):
        gui = Mock()
        gui.IsWindow.return_value = True
        with patch.dict('sys.modules', {'win32gui': gui}), \
             patch.object(self.backend, 'active_window', return_value={'hwnd': 99}):
            result = self.backend._window_mutation(42, 'close', self.cancel, target_hwnd=42)
        self.assertEqual(result['status'], 'clarification')
        gui.PostMessage.assert_not_called()

    def test_screenshot_uses_memory_and_clipboard(self):
        from PIL import Image
        image = Image.new('RGB', (2, 3), 'red')
        clipboard = Mock(CF_DIB=8)
        clipboard.IsClipboardFormatAvailable.return_value = True
        with patch('PIL.ImageGrab.grab', return_value=image), \
             patch.dict('sys.modules', {'win32clipboard': clipboard}):
            result = self.backend.execute('screenshot', {'target': 'screen'}, self.cancel)
        self.assertTrue(result['verified'])
        self.assertEqual(result['data']['width'], 2)
        self.assertEqual(clipboard.SetClipboardData.call_args.args[0], 8)
        self.assertIsInstance(clipboard.SetClipboardData.call_args.args[1], bytes)

    def test_active_screenshot_discards_capture_after_window_change(self):
        from PIL import Image
        window = dict(hwnd=42, rect=[0, 0, 2, 3])
        clipboard = Mock(CF_DIB=8)
        with patch('PIL.ImageGrab.grab', return_value=Image.new('RGB', (2, 3))), \
             patch.object(self.backend, 'active_window', side_effect=[window, window, {'hwnd': 99}]), \
             patch.dict('sys.modules', {'win32clipboard': clipboard}):
            result = self.backend.execute('screenshot', {'target': 'active'}, self.cancel, target_hwnd=42)
        self.assertEqual(result['status'], 'clarification')
        clipboard.OpenClipboard.assert_not_called()

    def test_active_screenshot_does_not_rebind_target_before_capture(self):
        a = dict(hwnd=42,rect=[0,0,2,3])
        b = dict(hwnd=99,rect=[10,10,2,3])
        clipboard = Mock(CF_DIB=8)
        with patch('PIL.ImageGrab.grab') as grab, \
             patch.object(self.backend,'active_window',side_effect=[a,b,b,b]), \
             patch.dict('sys.modules',{'win32clipboard':clipboard}):
            result=self.backend.execute('screenshot',{'target':'active'},self.cancel,target_hwnd=42)
        self.assertEqual(result['status'],'clarification')
        grab.assert_not_called()
        clipboard.OpenClipboard.assert_not_called()


if __name__ == '__main__':
    unittest.main()
