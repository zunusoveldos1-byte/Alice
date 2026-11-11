import time
import unittest
from threading import Event
from types import SimpleNamespace
from alice_voice.game_control import GameController


def token(text, x=20, y=20, w=100, h=20):
    return {'text': text, 'rect': [x, y, w, h]}


def frame(*tokens, rect=None, age=0):
    return {'hwnd': 7, 'rect': rect or [100, 200, 800, 600],
            'timestamp': time.monotonic() - age, 'tokens': list(tokens)}


class Desktop:
    def __init__(self):
        self.window = {'hwnd': 7, 'title': 'Game', 'rect': [100, 200, 800, 600]}
    def active_window(self):
        return self.window.copy()


class Observer:
    def __init__(self, frames, hook=None):
        self.frames = iter(frames)
        self.hook = hook
    def observe(self, window):
        value = next(self.frames)
        if self.hook:
            self.hook()
        return value


class Inputter:
    def __init__(self, fail=False):
        self.clicks = []
        self.fail = fail
        self.released = False
    def click(self, x, y, guard):
        guard()
        if self.fail:
            raise PermissionError('protected input')
        self.clicks.append((x, y))
    def release_all(self):
        self.released = True


class GameControlTests(unittest.TestCase):
    def run_game(self, frames, goal='open_settings', category='general', value=None,
                 cancel=None, hook=None, fail=False, permission=True):
        self.desktop = Desktop()
        self.inputter = Inputter(fail)
        settings = SimpleNamespace(permission_screen=permission, permission_input=permission,
                                   permission_games=permission)
        self.controller = GameController(settings, self.desktop, Observer(frames, hook), self.inputter)
        return self.controller.run(goal, category, value, cancel or Event(), 7)

    def test_permission_required(self):
        self.assertEqual(self.run_game([], permission=False)['status'], 'permission')

    def test_geoguessr_missing_settings_suggests_entering_game_without_input(self):
        desktop=Desktop();desktop.window.update(title='GeoGuessr - Game Client',exe='GeoGuessr.exe')
        inputter=Inputter()
        settings=SimpleNamespace(permission_screen=True,permission_input=True,permission_games=True)
        controller=GameController(settings,desktop,Observer([frame(token('Singleplayer'))]),inputter)
        result=controller.run('open_settings','general',None,Event(),7)
        self.assertEqual(result['status'],'clarification')
        self.assertIn('Войдите в игру',result['message'])
        self.assertIn('повторите',result['message'])
        self.assertFalse(result['verified'])
        self.assertEqual(inputter.clicks,[])

    def test_cancel_before_observe(self):
        cancel = Event(); cancel.set()
        self.assertEqual(self.run_game([], cancel=cancel)['status'], 'cancelled')
        self.assertEqual(self.inputter.clicks, [])

    def test_open_settings_reobserves_destination_and_uses_capture_coordinates(self):
        result = self.run_game([frame(token('Settings', 30, 40)),
                               frame(token('Settings'), token('Graphics', y=70))])
        self.assertTrue(result['verified'])
        self.assertEqual(self.inputter.clicks, [(180, 250)])
        self.assertTrue(self.inputter.released)

    def test_click_without_verified_destination_is_not_done(self):
        result = self.run_game([frame(token('Settings')), frame(token('Settings'))])
        self.assertEqual(result['status'], 'clarification')
        self.assertFalse(result['verified'])
        self.assertEqual(len(self.inputter.clicks), 1)

    def test_existing_settings_needs_no_click(self):
        result = self.run_game([frame(token('Settings'), token('Graphics', y=70))])
        self.assertTrue(result['verified'])
        self.assertEqual(self.inputter.clicks, [])

    def test_ambiguous_labels_prevent_input(self):
        result = self.run_game([frame(token('Settings'), token('Settings', y=90))])
        self.assertEqual(result['status'], 'clarification')
        self.assertEqual(self.inputter.clicks, [])

    def test_stale_capture_prevents_input(self):
        result = self.run_game([frame(token('Settings'), age=8)])
        self.assertEqual(result['status'], 'clarification')
        self.assertEqual(self.inputter.clicks, [])

    def test_window_switch_after_observe_prevents_input(self):
        def switch():
            self.desktop.window['hwnd'] = 8
        result = self.run_game([frame(token('Settings'))], hook=switch)
        self.assertEqual(result['status'], 'clarification')
        self.assertEqual(self.inputter.clicks, [])

    def test_move_after_observe_prevents_stale_coordinate_input(self):
        def move():
            self.desktop.window['rect'] = [300, 200, 800, 600]
        result = self.run_game([frame(token('Settings'))], hook=move)
        self.assertEqual(result['status'], 'clarification')
        self.assertEqual(self.inputter.clicks, [])

    def test_cancel_after_observe_prevents_input(self):
        cancel = Event()
        result = self.run_game([frame(token('Settings'))], cancel=cancel, hook=cancel.set)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.inputter.clicks, [])

    def test_protected_input_is_honest(self):
        result = self.run_game([frame(token('Settings'))], fail=True)
        self.assertEqual(result['status'], 'clarification')
        self.assertFalse(result['verified'])

    def test_parameter_without_supported_affordance_stops(self):
        result = self.run_game([frame(token('Music Volume'), token('50', x=200))],
                               goal='change_parameter', category='music_volume', value=60)
        self.assertEqual(result['status'], 'clarification')
        self.assertEqual(result['data']['current_value'], 50)
        self.assertEqual(self.inputter.clicks, [])

    def test_single_parameter_plus_then_verified_value(self):
        result = self.run_game([frame(token('Music Volume'), token('50', x=200), token('+', x=330, w=20)),
                               frame(token('Music Volume'), token('60', x=200), token('+', x=330, w=20))],
                               goal='change_parameter', category='music_volume', value=60)
        self.assertTrue(result['verified'])
        self.assertEqual(len(self.inputter.clicks), 1)

    def test_resolution_returns_preview_no_input(self):
        result = self.run_game([frame(token('Resolution'), token('1920x1080', x=200))],
                               goal='change_parameter', category='resolution', value=1080)
        self.assertEqual(result['status'], 'clarification')
        self.assertIn('preview', result['data'])
        self.assertEqual(self.inputter.clicks, [])

    def test_no_arbitrary_goal_or_category(self):
        result = self.run_game([], goal='press_alt_f4')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(self.inputter.clicks, [])

    def test_visible_category_tab_does_not_prove_category_open(self):
        result = self.run_game([frame(token('Settings'), token('Graphics', y=70))], category='graphics')
        self.assertFalse(result['verified'])

    def test_menu_click_must_show_expected_destination(self):
        result = self.run_game([frame(token('Settings')),
                               frame(token('Main Menu'), token('Settings', y=80))])
        self.assertEqual(result['status'], 'clarification')
        self.assertEqual(len(self.inputter.clicks), 1)

    def test_only_four_parameter_actions_allowed(self):
        frames = [frame(token('Music Volume'), token(str(n), x=200), token('+', x=330, w=20)) for n in range(5)]
        result = self.run_game(frames, goal='change_parameter', category='music_volume', value=10)
        self.assertEqual(result['status'], 'clarification')
        self.assertEqual(len(self.inputter.clicks), 4)

    def test_parameter_overshoot_not_claimed_success(self):
        frames = [frame(token('Music Volume'), token(str(n), x=200), token('+', x=330, w=20)) for n in (50, 70)]
        result = self.run_game(frames, goal='change_parameter', category='music_volume', value=60)
        self.assertEqual(result['status'], 'clarification')
        self.assertEqual(len(self.inputter.clicks), 1)

    def test_inputter_final_guard_catches_last_moment_window_switch(self):
        desktop = Desktop()
        inputter = Inputter()
        def guarded_click(x, y, guard):
            desktop.window['hwnd'] = 99
            guard()
            inputter.clicks.append((x, y))
        inputter.click = guarded_click
        settings = SimpleNamespace(permission_screen=True, permission_input=True, permission_games=True)
        controller = GameController(settings, desktop, Observer([frame(token('Settings'))]), inputter)
        result = controller.run('open_settings', 'general', None, Event(), 7)
        self.assertEqual(result['status'], 'clarification')
        self.assertEqual(inputter.clicks, [])
        self.assertTrue(inputter.released)

    def test_unknown_screen_never_sends_escape(self):
        result = self.run_game([frame(token('Player health 100'))], goal='back')
        self.assertEqual(result['status'], 'clarification')
        self.assertEqual(result['data']['observed_state'], 'unknown')
        self.assertEqual(self.inputter.clicks, [])

    def test_change_category_requires_specific_parameter_before_input(self):
        result = self.run_game([frame(token('Settings'))], goal='change_parameter', category='graphics', value=50)
        self.assertEqual(result['status'], 'clarification')
        self.assertEqual(self.inputter.clicks, [])

    def test_relative_decrease_derives_target_from_observed_value(self):
        desktop = Desktop(); inputter = Inputter()
        frames = [frame(token('Music Volume'), token(str(n), x=200), token('-', x=330, w=20)) for n in (50, 40)]
        settings = SimpleNamespace(permission_screen=True, permission_input=True, permission_games=True)
        controller = GameController(settings, desktop, Observer(frames), inputter)
        result = controller.run('change_parameter', 'music_volume', None, Event(), 7, direction='down')
        self.assertTrue(result['verified'])
        self.assertEqual(result['data']['target_value'], 40)
        self.assertEqual(inputter.clicks, [(440, 230)])

    def test_relative_increase_clamps_music_volume_to_100(self):
        desktop = Desktop(); inputter = Inputter()
        frames = [frame(token('Music Volume'), token(str(n), x=200), token('+', x=330, w=20)) for n in (95, 100)]
        settings = SimpleNamespace(permission_screen=True, permission_input=True, permission_games=True)
        controller = GameController(settings, desktop, Observer(frames), inputter)
        result = controller.run('change_parameter', 'music_volume', 20, Event(), 7, direction='up')
        self.assertTrue(result['verified'])
        self.assertEqual(result['data']['target_value'], 100)
        self.assertEqual(len(inputter.clicks), 1)

    def test_relative_target_is_derived_once_across_multiple_clicks(self):
        desktop = Desktop(); inputter = Inputter()
        frames = [frame(token('Music Volume'), token(str(n), x=200), token('+', x=330, w=20)) for n in (40,45,50)]
        settings = SimpleNamespace(permission_screen=True, permission_input=True, permission_games=True)
        controller = GameController(settings, desktop, Observer(frames), inputter)
        result = controller.run('change_parameter', 'music_volume', None, Event(), 7, direction='up')
        self.assertTrue(result['verified'])
        self.assertEqual(result['data']['target_value'], 50)
        self.assertEqual(len(inputter.clicks), 2)

    def test_unknown_direction_and_negative_relative_steps_never_click(self):
        for direction, value in [('sideways',10), ('down',-10)]:
            desktop = Desktop(); inputter = Inputter()
            settings = SimpleNamespace(permission_screen=True, permission_input=True, permission_games=True)
            controller = GameController(settings, desktop, Observer([]), inputter)
            result = controller.run('change_parameter', 'music_volume', value, Event(), 7, direction=direction)
            self.assertEqual(result['status'], 'error')
            self.assertEqual(inputter.clicks, [])


class NativeOCRTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        import os
        if os.name != 'nt' or importlib.util.find_spec('winrt') is None:
            self.skipTest('Optional Windows local OCR runtime unavailable')
        for name in ('winrt.windows.foundation', 'winrt.windows.foundation.collections',
                     'winrt.windows.media.ocr', 'winrt.windows.graphics.imaging',
                     'winrt.windows.storage.streams'):
            if importlib.util.find_spec(name) is None:
                self.skipTest('Optional Windows local OCR projection unavailable')

    def test_local_ocr_reads_only_synthetic_image_and_initializes_apartment(self):
        import asyncio
        from unittest.mock import patch
        from PIL import Image, ImageDraw, ImageFont
        from winrt.runtime import init_apartment
        from alice_voice.game_control import LocalOCRObserver
        image = Image.new('RGBA', (800,200), 'white')
        ImageDraw.Draw(image).text((25,35), 'Settings',
                                  font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',64), fill='black')
        with patch('winrt.runtime.init_apartment', wraps=init_apartment) as initialize:
            tokens = asyncio.run(LocalOCRObserver()._recognize(image.tobytes('raw','BGRA'),800,200))
        self.assertTrue(initialize.called)
        self.assertTrue(any(t['text'].casefold() == 'settings' for t in tokens))
        self.assertTrue(all(t['rect'][0] >= 0 and t['rect'][1] >= 0 for t in tokens))

    def test_ocr_recognition_has_explicit_async_timeout(self):
        import asyncio
        import sys
        from unittest.mock import patch
        from alice_voice.game_control import LocalOCRObserver
        class SlowEngine:
            max_image_dimension = 10000
            @classmethod
            def try_create_from_user_profile_languages(cls):
                return cls()
            async def recognize_async(self, bitmap):
                await asyncio.sleep(2)
        with patch.dict(sys.modules, {'winrt.windows.media.ocr': SimpleNamespace(OcrEngine=SlowEngine)}):
            with self.assertRaises(TimeoutError):
                asyncio.run(LocalOCRObserver(ocr_timeout=.01)._recognize(bytes(20*20*4),20,20))
