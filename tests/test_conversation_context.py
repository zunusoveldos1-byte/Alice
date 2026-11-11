import unittest
from threading import Event
from unittest.mock import Mock


class ConversationTests(unittest.TestCase):
    def test_short_dialogue_is_exact_and_has_no_summary(self):
        from alice_voice.conversation import ConversationContext
        c = ConversationContext(history_turns=2)
        c.commit('Объясни фотосинтез.', 'Растения используют свет.')
        self.assertEqual(c.messages(), [
            {'role': 'user', 'content': 'Объясни фотосинтез.'},
            {'role': 'assistant', 'content': 'Растения используют свет.'}])
        self.assertEqual(c.summary, '')

    def test_eviction_preserves_whole_correction_and_attributes_assistant_claim(self):
        from alice_voice.conversation import ConversationContext
        c = ConversationContext(history_turns=1)
        c.commit('Нет, используй документ Отчёт.docx, а не План.docx.', 'Я открыла документ.')
        c.commit('Объясни подробнее.', 'Подробнее о документе.')
        self.assertIn('Нет, используй документ Отчёт.docx, а не План.docx.', c.summary)
        self.assertIn('Ответ ассистента', c.summary)
        self.assertEqual(c.task.completed_steps, [])
        self.assertEqual(list(c.recent)[-2]['content'], 'Объясни подробнее.')

    def test_character_budget_compacts_before_turn_count_and_keeps_decisions(self):
        from alice_voice.conversation import ConversationContext
        c = ConversationContext(history_turns=20, recent_budget=500, summary_budget=700)
        c.commit('Решили использовать Firefox.', 'Хорошо.')
        for i in range(30):
            c.commit(f'Тема {i}: ' + 'Общее объяснение. ' * 5, 'Детали. ' * 10)
        self.assertLessEqual(sum(len(m['content']) for m in c.recent), 500)
        self.assertLessEqual(len(c.summary), 700)
        self.assertIn('Решили использовать Firefox.', c.summary)

    def test_task_never_marks_submitted_failed_or_cancelled_as_completed(self):
        from alice_voice.conversation import TaskState
        from alice_voice.context_tools import ToolResult
        task = TaskState()
        task.begin('Открой документ', '1')
        task.record('launch_app', {'name': 'Документ'}, ToolResult('done', 'Запрос передан.'), '1')
        self.assertEqual(task.completed_steps, [])
        self.assertEqual(task.last_action['status'], 'submitted')
        task.begin('Нет, другой', '2')
        task.record('launch_app', {}, ToolResult('done', 'Старое действие', verified=True), '1')
        self.assertNotEqual(task.last_action['message'], 'Старое действие')
        task.record('launch_app', {}, ToolResult('cancelled', 'Отменено'), '2')
        self.assertEqual(task.completed_steps, [])

    def test_memory_context_is_cached_and_mutations_invalidate(self):
        from alice_voice.assistant_actions import AssistantActions
        from alice_voice.config import Settings
        memory = Mock()
        memory.context.return_value = 'Пользователь предпочитает короткие ответы.'
        memory.remember.return_value = 1
        actions = AssistantActions(Settings(), memory=memory)
        self.assertEqual(actions.context(), actions.context())
        memory.context.assert_called_once()
        actions.handle('Запомни любит чай', Event())
        actions.context()
        self.assertEqual(memory.context.call_count, 2)


if __name__ == '__main__':
    unittest.main()
