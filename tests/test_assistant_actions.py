import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import TestCase
from unittest.mock import Mock, patch
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from time import sleep

from alice_voice.config import Settings
from alice_voice.assistant_actions import AssistantActions, parse_action, search_url
from alice_voice.memory import FactMemory


class ActionTests(TestCase):
    def test_commands_require_explicit_request_and_never_execute_arbitrary_text(self):
        self.assertEqual(parse_action('Открой блокнот!'), ('app', 'блокнот'))
        for text in ('Как открыть блокнот?', 'Не открывай блокнот', 'Открой cmd /c del *', 'Расскажи про Windows'):
            self.assertIsNone(parse_action(text))

    def test_spoken_numbers_work_for_volume_and_fact_deletion(self):
        self.assertEqual(parse_action('забудь факт один'), ('forget', 1))
        self.assertEqual(parse_action('сделай громкость пятьдесят пять процентов'), ('audio', ('set', 55)))
        self.assertEqual(parse_action('найди на сайте википедия силу тяжести'), ('search', ('силу тяжести', 'ru.wikipedia.org')))

    def test_cancel_prevents_windows_side_effect(self):
        with TemporaryDirectory() as folder:
            backend = Mock()
            actions = AssistantActions(Settings(), backend=backend, memory=FactMemory(Path(folder)/'m.db'))
            cancel = Event(); cancel.set()
            with self.assertRaises(asyncio.CancelledError): actions.handle('Открой блокнот', cancel)
            backend.open_app.assert_not_called()

    def test_disabled_commands_are_not_sent_to_model_as_success(self):
        with TemporaryDirectory() as folder:
            backend = Mock()
            actions = AssistantActions(Settings(desktop_commands=False), backend=backend, memory=FactMemory(Path(folder)/'m.db'))
            self.assertIn('отключены', actions.handle('Открой блокнот', Event()))
            backend.open_app.assert_not_called()

    def test_result_comes_from_adapter_and_unknown_request_falls_through(self):
        with TemporaryDirectory() as folder:
            backend = Mock(); backend.open_app.return_value = 'Запущен Блокнот.'
            actions = AssistantActions(Settings(),backend=backend,memory=FactMemory(Path(folder)/'m.db'))
            cancel = Event()
            self.assertEqual(actions.handle('Открой блокнот',cancel),'Запущен Блокнот.')
            backend.open_app.assert_called_once_with('блокнот', cancel)
            self.assertIsNone(actions.handle('Объясни фотосинтез',Event()))

    def test_search_query_is_url_encoded_and_domain_is_validated(self):
        self.assertIn('q=',search_url('кот & собака'))
        self.assertIn('site%3Amozilla.org',search_url('справка','mozilla.org'))
        for domain in ('localhost','127.0.0.1','x.com & calc','https://user:pass@example.com','file:///C:/'):
            with self.assertRaises(ValueError): search_url('x',domain)

    def test_search_launches_only_firefox_with_separate_url_argument(self):
        with TemporaryDirectory() as folder:
            firefox=Path(folder)/'firefox.exe';firefox.write_bytes(b'placeholder')
            actions=AssistantActions(Settings(firefox_path=str(firefox)),memory=FactMemory(Path(folder)/'m.db'))
            with patch('alice_voice.assistant_actions.subprocess.Popen') as launch:
                result=actions.handle('Найди на сайте mozilla.org настройки приватности',Event())
                self.assertIn('Firefox',result)
                args=launch.call_args.args[0]
                self.assertEqual(args[:2],[str(firefox),'-new-tab'])
                self.assertIn('site%3Amozilla.org',args[2]);self.assertFalse(launch.call_args.kwargs['shell'])


class MemoryTests(TestCase):
    def test_cancellation_during_database_lock_does_not_save_fact(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/'memory.db';memory=FactMemory(path);memory.remember('existing')
            cancel=Event()
            connection=sqlite3.connect(path);connection.execute('BEGIN EXCLUSIVE')
            try:
                with ThreadPoolExecutor(1) as executor:
                    task=executor.submit(memory.remember,'cancelled',cancel)
                    sleep(.1);cancel.set();connection.rollback()
                    with self.assertRaises(asyncio.CancelledError): task.result(4)
            finally:
                connection.close()
            self.assertEqual([text for _,text in memory.list()],['existing'])
    def test_explicit_facts_persist_deduplicate_and_can_be_erased(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/'memory.db'; m=FactMemory(path)
            identity=m.remember('Меня зовут Элдос')
            self.assertEqual(m.remember('Меня зовут Элдос'),identity)
            self.assertEqual(FactMemory(path).list(),[(identity,'Меня зовут Элдос')])
            self.assertTrue(m.forget(identity)); self.assertFalse(m.forget(identity));self.assertEqual(m.list(),[])
            m.remember('Люблю Python');m.clear();self.assertEqual(m.list(),[])

    def test_disabled_memory_does_not_create_database(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/'memory.db'
            actions=AssistantActions(Settings(memory_enabled=False),memory=FactMemory(path))
            self.assertIn('отключена',actions.handle('Запомни: меня зовут Элдос',Event()))
            self.assertFalse(path.exists());self.assertEqual(actions.context(),'')

    def test_remember_recall_delete_and_bounded_context(self):
        with TemporaryDirectory() as folder:
            actions=AssistantActions(Settings(),memory=FactMemory(Path(folder)/'m.db'))
            self.assertIn('Запомнила',actions.handle('Запомни: мой любимый язык Python',Event()))
            self.assertIn('Python',actions.handle('Что ты помнишь',Event()))
            self.assertIn('Python',actions.context())
            self.assertIn('очищена',actions.handle('Очисти всю память',Event()))
            self.assertEqual(actions.context(),'')
