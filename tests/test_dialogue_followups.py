import asyncio
from threading import Event
import unittest
from unittest.mock import Mock, patch

import httpx
from alice_voice.config import Settings
from alice_voice.context_tools import ToolRouter, ToolResult


class Followups(unittest.IsolatedAsyncioTestCase):
    def make(self):
        backend = Mock()
        backend.active_window.return_value = {'hwnd': 22, 'pid': 999, 'exe': 'firefox.exe', 'title': 'Firefox'}
        backend.execute.return_value = {'status': 'done', 'message': 'Открыто', 'verified': True}
        return ToolRouter(Settings(), desktop=backend), backend

    async def test_browser_followup_checks_fresh_window_before_search(self):
        router, backend = self.make()
        with patch.object(router, '_browser', return_value=ToolResult('done', 'Запрос открытия вкладки передан.')) as browser:
            await router.handle('Открой браузер', Event(), turn_id='1')
            self.assertEqual(browser.call_count, 1)
            result = await router.handle('А теперь найди там прогноз погоды', Event(), turn_id='2')
            self.assertEqual(result.status, 'done')
            self.assertEqual(browser.call_args.args[:2], ('browser_search', {'query': 'прогноз погоды'}))
            backend.active_window.return_value = {'hwnd': 33, 'pid': 998, 'exe': 'notepad.exe', 'title': 'Блокнот'}
            result = await router.handle('А теперь найди там новости', Event(), turn_id='3')
            self.assertEqual(result.status, 'clarification')
            self.assertEqual(browser.call_count, 2)

    async def test_ordinal_choice_and_ambiguous_reference(self):
        router, backend = self.make()
        router.pending_choice = {'choices': [{'name': 'Один'}, {'name': 'Два'}], 'tool': 'launch_app', 'arguments': {}}
        await router.handle('Выбери вторую', Event(), turn_id='1')
        self.assertEqual(backend.execute.call_args.args[1], {'name': 'Два'})
        result = await router.handle('Выбери вторую', Event(), turn_id='2')
        self.assertEqual(result.status, 'clarification')

    async def test_document_correction_never_opens_arbitrary_replacement(self):
        router, backend = self.make()
        await router.handle('Открой документ', Event(), turn_id='1')
        backend.execute.reset_mock()
        result = await router.handle('Нет, другой', Event(), turn_id='2')
        self.assertEqual(result.status, 'clarification')
        backend.execute.assert_not_called()
        self.assertIn('документ', result.message.casefold())

    async def test_number_in_new_command_is_not_a_pending_selection(self):
        router, backend = self.make()
        router.pending_choice = {'choices': [{'name': 'Один'}, {'name': 'Два'}], 'tool': 'launch_app', 'arguments': {}}
        await router.handle('Установи громкость на 2 процента', Event(), turn_id='1')
        self.assertEqual(backend.execute.call_args.args[0], 'volume')
        self.assertEqual(backend.execute.call_args.args[1], {'mode': 'set', 'value': 2})
        self.assertIsNone(router.pending_choice)

    async def test_late_tool_result_cannot_publish_success(self):
        router, backend = self.make()
        cancel = Event()
        def late(*args):
            cancel.set()
            return {'status': 'done', 'message': 'Late result', 'verified': True}
        backend.execute.side_effect = late
        with self.assertRaises(asyncio.CancelledError):
            await router.handle('Открой блокнот', cancel, turn_id='1')
        self.assertFalse(any(row['status'] == 'done' for row in router.journal))

    async def test_failed_window_observation_does_not_reuse_stale_focus(self):
        router, backend = self.make()
        router.observe_context()
        backend.active_window.side_effect = RuntimeError('unavailable')
        self.assertEqual(router.observe_context(), {})
        result = await router.handle('Закрой это окно', Event(), turn_id='1')
        self.assertEqual(result.status, 'clarification')
        backend.execute.assert_not_called()

    async def test_models_list_then_select_second_uses_installed_ids(self):
        def handler(request):
            return httpx.Response(200, json={'models': [{'name': 'qwen3.5:4b'}, {'name': 'qwen3:1.7b'}]})
        from alice_voice.providers import LLMProvider
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router, backend = self.make()
            router.provider = LLMProvider(Settings(), client)
            result = await router.handle('Покажи доступные модели', Event(), turn_id='1')
            self.assertIn('qwen3.5:4b', result.message)
            result = await router.handle('Выбери вторую', Event(), turn_id='2')
            self.assertEqual(result.data['selected_model'], 'qwen3:1.7b')
            self.assertNotEqual(result.status, 'done')
            backend.execute.assert_not_called()

    async def test_stop_clears_pending_owner_change(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from alice_voice.memory import FactMemory
        with TemporaryDirectory() as directory:
            router, backend = self.make()
            router.legacy.memory = FactMemory(Path(directory) / 'memory.sqlite3')
            await router.handle('Запомни, меня зовут Элдос', Event(), turn_id='1')
            await router.handle('Запомни, меня зовут Игорь', Event(), turn_id='2')
            self.assertIsNotNone(router.legacy.pending_name)
            router.stop()
            self.assertIsNone(router.legacy.pending_name)
            await router.handle('Да', Event(), turn_id='3')
            self.assertEqual(router.legacy.memory.owner_name(), 'Элдос')

    async def test_rejected_memory_request_is_not_a_completed_action(self):
        router, backend = self.make()
        result = await router.handle('Запомни: это проверка памяти', Event(), turn_id='1')
        self.assertNotEqual(result.status, 'done')
        self.assertEqual(router.task.completed_steps, [])
