"""Background metadata throttling must never replace command-time focus checks."""
import asyncio
from threading import Event
import unittest
from unittest.mock import Mock, patch

from alice_voice.config import Settings
from alice_voice.context_tools import ToolRouter
from alice_voice.desktop_backend import DesktopBackend
from alice_voice.metrics import LatencyMetrics
from alice_voice.workers import DialogRuntime, WorkerSignals


def desktop():
    result = Mock()
    result.foreground_window_id.return_value = 10
    result.active_window.return_value = {'hwnd': 10, 'pid': -1, 'title': 'Game', 'exe': 'game.exe', 'rect': [0,0,640,480]}
    return result


class BackgroundPollingTests(unittest.TestCase):
    def test_stable_handle_reads_metadata_once_per_five_seconds(self):
        backend=desktop(); router=ToolRouter(Settings(),desktop=backend)
        with patch('alice_voice.context_tools.monotonic') as clock:
            for step in range(11):
                clock.return_value=step*.5
                router.poll_context()
        self.assertEqual(backend.foreground_window_id.call_count,11)
        self.assertEqual(backend.active_window.call_count,2)

    def test_focus_change_refreshes_immediately_and_title_refreshes_on_same_handle(self):
        backend=desktop(); router=ToolRouter(Settings(),desktop=backend)
        with patch('alice_voice.context_tools.monotonic',return_value=0): router.poll_context()
        backend.foreground_window_id.return_value=20
        backend.active_window.return_value={'hwnd':20,'pid':-1,'title':'Document','exe':'editor.exe'}
        with patch('alice_voice.context_tools.monotonic',return_value=.5): router.poll_context()
        self.assertEqual(router.last_context['hwnd'],20)
        backend.active_window.return_value['title']='Changed title'
        with patch('alice_voice.context_tools.monotonic',return_value=5.5): router.poll_context()
        self.assertEqual(router.last_context['title'],'Changed title')
        self.assertEqual(backend.active_window.call_count,3)

    def test_metadata_failure_is_throttled_and_retried(self):
        backend=desktop(); router=ToolRouter(Settings(),desktop=backend)
        backend.active_window.side_effect=[OSError('unavailable'),backend.active_window.return_value]
        with patch('alice_voice.context_tools.monotonic') as clock:
            for now in (0,.5,1,5):
                clock.return_value=now; router.poll_context()
        self.assertEqual(backend.active_window.call_count,2)
        self.assertEqual(router.last_context['hwnd'],10)

    def test_handle_query_failure_falls_back_to_throttled_full_observation(self):
        backend=desktop(); router=ToolRouter(Settings(),desktop=backend)
        backend.foreground_window_id.side_effect=OSError('unavailable')
        with patch('alice_voice.context_tools.monotonic') as clock:
            for now in (0,.5,1,5):
                clock.return_value=now; router.poll_context()
        self.assertEqual(backend.active_window.call_count,2)

    def test_handle_check_does_not_query_process_or_window_metadata(self):
        with patch('win32gui.GetForegroundWindow',return_value=20), \
             patch.object(DesktopBackend,'_window') as metadata:
            self.assertEqual(DesktopBackend(Settings()).foreground_window_id(),20)
        metadata.assert_not_called()


class CommandFocusTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_checks_fresh_focus_even_inside_background_refresh_interval(self):
        backend=desktop(); router=ToolRouter(Settings(),desktop=backend)
        with patch('alice_voice.context_tools.monotonic',return_value=0): router.poll_context()
        old=dict(router.last_context)
        backend.active_window.return_value={'hwnd':20,'pid':-1,'title':'Other window'}
        result=await router.execute('window_control',{'mode':'close'},Event(),context=old)
        self.assertEqual(result.status,'clarification')
        self.assertEqual(backend.active_window.call_count,2)
        backend.execute.assert_not_called()

    async def test_runtime_background_uses_throttled_observation(self):
        runtime=DialogRuntime(Settings(),WorkerSignals(),LatencyMetrics())
        def observed(): runtime.shutdown_requested=True
        runtime.router=Mock(poll_context=Mock(side_effect=observed))
        await asyncio.wait_for(runtime._watch_context(),1)
        runtime.router.poll_context.assert_called_once()
        runtime.router.observe_context.assert_not_called()
