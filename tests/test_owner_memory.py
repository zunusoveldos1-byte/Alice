from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import sqlite3
import unittest

from alice_voice.memory import FactMemory
from alice_voice.assistant_actions import AssistantActions
from alice_voice.config import Settings


class OwnerMemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'memory.sqlite3'
        self.memory = FactMemory(self.path)
        self.actions = AssistantActions(Settings(data_directory=self.directory.name), memory=self.memory)

    def test_required_sequence_and_restart_never_uses_model_as_identity(self):
        self.actions.handle('Запомни, меня зовут Элдос', Event())
        for text in ('Привет, это проверка памяти', 'Моего друга зовут Андрей', 'Представь, что я Игорь',
                     'Меня зовут Андрей', 'Запомни пример: я Игорь', 'Запомни: Привет, это проверка памяти'):
            self.actions.handle(text, Event())
            self.assertEqual(self.memory.owner_name(), 'Элдос')
        restarted = AssistantActions(Settings(), memory=FactMemory(self.path))
        self.assertEqual(restarted.handle('Как меня зовут?', Event()), 'В профиле владельца сохранено имя Элдос.')
        self.assertNotIn('Игорь', self.memory.context())
        self.assertNotIn('проверка памяти', self.memory.context())

    def test_name_change_requires_separate_confirmation_and_current_revision(self):
        self.actions.handle('Запомни, меня зовут Элдос', Event(), turn_id='1')
        result = self.actions.handle('Запомни, меня зовут Игорь', Event(), turn_id='2')
        self.assertEqual(result, 'Сейчас сохранено имя Элдос. Заменить его на Игорь?')
        self.actions.handle('Да', Event(), turn_id='2')
        self.assertEqual(self.memory.owner_name(), 'Элдос')
        self.actions.handle('Нет', Event(), turn_id='3')
        self.assertEqual(self.memory.owner_name(), 'Элдос')
        self.actions.handle('Запомни, меня зовут Игорь', Event(), turn_id='4')
        self.actions.handle('Да', Event(), turn_id='5')
        self.assertEqual(self.memory.owner_name(), 'Игорь')

    def test_late_confirmation_cannot_overwrite_newer_ui_edit(self):
        self.actions.handle('Запомни, меня зовут Элдос', Event(), turn_id='1')
        self.actions.handle('Запомни, меня зовут Игорь', Event(), turn_id='2')
        row = self.memory.records()[0]
        self.memory.edit(row['id'], 'Элдос', expected_revision=row['revision'], source='user_settings')
        result = self.actions.handle('Да', Event(), turn_id='3')
        self.assertIn('измен', result.lower())
        self.assertEqual(self.memory.owner_name(), 'Элдос')

    def test_metadata_audit_conflicts_and_profile_isolation(self):
        self.memory.remember('Меня зовут Элдос')
        row = self.memory.records()[0]
        self.assertEqual(row['category'], 'profile')
        for key in ('id', 'source', 'updated_at', 'status', 'revision', 'value'):
            self.assertIn(key, row)
        self.assertEqual(row['status'], 'confirmed')
        self.assertEqual(FactMemory(self.path, profile_id='guest').owner_name(), '')
        self.memory.edit(row['id'], 'Элдос', expected_revision=row['revision'], source='user_settings')
        with self.assertRaises(ValueError):
            self.memory.edit(row['id'], 'Игорь', expected_revision=row['revision'], source='user_settings')
        with self.assertRaises(ValueError):
            self.memory.remember('Меня зовут Андрей', source='model_response')
        self.assertTrue(self.memory.audit())
        self.assertNotIn('text', self.memory.audit()[0])

    def test_unknown_name_is_answered_without_guessing(self):
        answer = self.actions.handle('Как меня зовут?', Event())
        self.assertIn('ещё не сохранено', answer)

    def test_profession_and_mood_are_not_owner_names(self):
        self.actions.handle('Запомни: я программист', Event())
        self.assertEqual(self.memory.owner_name(), '')
        self.actions.handle('Запомни, меня зовут Элдос', Event())
        self.assertIsNone(self.actions.handle('Я устал', Event()))
        self.assertIsNone(self.actions.pending_name)
        self.assertEqual(self.memory.owner_name(), 'Элдос')

    def test_legacy_entries_are_not_silently_confirmed(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE facts (id INTEGER PRIMARY KEY, text TEXT UNIQUE NOT NULL)')
            db.executemany('INSERT INTO facts(text) VALUES (?)', [('меня зовут и дос',), ('это проверка постоянной память',), ('Моего друга зовут Андрей',)])
        db.close()
        self.assertEqual(self.memory.owner_name(), '')
        self.assertEqual(self.memory.context(), '')
        rows = self.memory.records(include_inactive=True)
        self.assertEqual(len(rows), 3)
        self.assertTrue(any('друга' in row['value'] for row in rows))
        self.assertTrue(all(row['status'] != 'confirmed' for row in rows))

    def test_cancelled_write_and_summary_cannot_change_profile(self):
        self.memory.remember('Меня зовут Элдос')
        cancel = Event(); cancel.set()
        with self.assertRaises(BaseException):
            self.memory.remember('Люблю чай', cancel)
        self.memory.save_summary('Модель предположила, что пользователь Андрей.', conversation_id='test')
        self.assertEqual(self.memory.owner_name(), 'Элдос')
        self.assertNotIn('Андрей', self.memory.context())
