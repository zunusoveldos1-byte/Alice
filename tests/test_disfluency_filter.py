"""Russian disfluency rules and the final STT-to-LLM boundary."""
from time import perf_counter
import unittest

from PyQt6.QtCore import QCoreApplication

from alice_voice.config import Settings
from alice_voice.controller import VoiceSessionController


APP = QCoreApplication.instance() or QCoreApplication([])


class DisfluencyFilterTests(unittest.TestCase):
    def cleaner(self):
        from alice_voice.utils.disfluency_filter import DisfluencyFilter
        return DisfluencyFilter()

    def test_removes_fillers_and_repairs_spacing(self):
        cases = [
            ('Эээ, ну, расскажи, короче, как бы про Python типа.', 'расскажи про Python.'),
            ('ммм ну какая погода', 'какая погода'),
            ('Ну... включи музыку!', 'включи музыку!'),
            ('скажи э-э-э сколько будет два плюс два', 'скажи сколько будет два плюс два'),
            ('покажи, ну, прогноз на завтра', 'покажи прогноз на завтра'),
            ('эээ\nммм\tну короче как   бы типа', ''),
            ('   ', ''), ('', ''),
        ]
        cleaner = self.cleaner()
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(cleaner.clean(raw), expected)

    def test_new_commands_replace_previous_thought_and_trailing_markers_cancel(self):
        cases = [
            ('погода в Москве ой нет погода в Бишкеке', 'погода в Бишкеке'),
            ('поставь таймер, точнее, будильник на восемь', 'поставь будильник на восемь'),
            ('включи музыку а хотя расскажи новости', 'расскажи новости'),
            ('покажи календарь забудь открой заметки', 'открой заметки'),
            ('Москва ой, нет, Казань точнее эээ Бишкек', 'Бишкек'),
            ('сегодня А ХОТЯ, ну, завтра', 'завтра'),
            ('покажи погоду ой нет', ''),
            ('забудь', ''),
        ]
        cleaner = self.cleaner()
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(cleaner.clean(raw), expected)

    def test_preserves_word_boundaries_quotes_and_negated_reminders(self):
        cases = [
            'минуту подумай о типах и нумерации',
            'не забудь купить молоко',
            'НЕ ЗАБУДЬ про встречу',
            'объясни выражение «ой нет»',
            'что значит «ну, типа, как бы»?',
            'переведи "забудь" на английский',
            'Погода в Бишкеке?',
            'сколько будет 1,5 плюс 2,5?',
            'Как бы ты решил эту задачу?',
        ]
        cleaner = self.cleaner()
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(cleaner.clean(text), text)
        self.assertEqual(cleaner.clean('короче говоря, расскажи про «точнее»'), 'говоря, расскажи про «точнее»')

    def test_calls_are_independent_and_cleaning_is_idempotent(self):
        cleaner = self.cleaner()
        self.assertEqual(cleaner.clean('погода ой нет'), '')
        result = cleaner.clean('ну расскажи про Python точнее про asyncio')
        self.assertEqual(result, 'расскажи про asyncio')
        self.assertEqual(cleaner.clean(result), result)
        self.assertEqual(cleaner.clean('Погода завтра?'), 'Погода завтра?')

    def test_repairs_slots_without_losing_the_request(self):
        cases = [
            ('поставь таймер на семь, точнее на восемь', 'поставь таймер на восемь'),
            ('поставь таймер на семь ой нет на восемь точнее на девять', 'поставь таймер на девять'),
            ('расскажи про Python, точнее про asyncio', 'расскажи про asyncio'),
            ('какая погода в Москве ой нет в Бишкеке', 'какая погода в Бишкеке'),
            ('поставь таймер на 5 минут точнее 10', 'поставь таймер на 10 минут'),
            ('поставь таймер на пять минут точнее десять минут', 'поставь таймер на десять минут'),
            ('поставь будильник на 7:30 точнее 8:45', 'поставь будильник на 8:45'),
            ('напомни завтра купить молоко точнее послезавтра', 'напомни послезавтра купить молоко'),
            ('напомни 21.10.2026 про встречу точнее 22.10.2026', 'напомни 22.10.2026 про встречу'),
            ('напомни 21.10.2026 в 7:30 точнее 22.10.2026', 'напомни 22.10.2026 в 7:30'),
            ('напомни в 7:30 21.10.2026 точнее 8:45', 'напомни в 8:45 21.10.2026'),
            ('поставь таймер на двадцать пять минут точнее тридцать', 'поставь таймер на тридцать минут'),
            ('включи свет ой нет не включай свет', 'не включай свет'),
            ('не включай свет точнее включи свет', 'включи свет'),
            ('поставь таймер на семь забудь расскажи новости', 'расскажи новости'),
        ]
        cleaner = self.cleaner()
        for raw, expected in cases:
            with self.subTest(raw=raw):
                result = cleaner.clean(raw)
                self.assertEqual(result, expected)
                self.assertEqual(cleaner.clean(result), result)

    def test_meaningful_uses_of_filler_words_are_preserved(self):
        cleaner = self.cleaner()
        for text in ('объясни данные типа int', 'какого типа эта переменная?',
                     'сделай ответ короче', 'не забудь купить молоко',
                     'не включай музыку', 'что значит слово типа'):
            with self.subTest(text=text):
                self.assertEqual(cleaner.clean(text), text)

    def test_adjacent_corrections_keep_the_original_intent(self):
        cases = [
            ('Поставь таймер на семь... ой нет, точнее на восемь минут',
             'Поставь таймер на восемь минут'),
            ('поставь таймер на семь ой нет точнее восемь',
             'поставь таймер на восемь'),
            ('напомни завтра купить молоко ой нет, эээ, точнее послезавтра',
             'напомни послезавтра купить молоко'),
            ('какая погода в Москве ой нет, точнее в Бишкеке',
             'какая погода в Бишкеке'),
        ]
        cleaner = self.cleaner()
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(cleaner.clean(raw), expected)
                self.assertEqual(cleaner.clean(expected), expected)

    def test_adjacent_trailing_corrections_and_forget_still_cancel(self):
        cleaner = self.cleaner()
        for raw in ('покажи погоду ой нет, точнее',
                    'покажи погоду ой нет, точнее ну эээ',
                    'покажи погоду забудь ой нет точнее'):
            with self.subTest(raw=raw):
                self.assertEqual(cleaner.clean(raw), '')
        self.assertEqual(cleaner.clean('поставь таймер на семь забудь точнее на восемь'),
                         'на восемь')

    def test_type_of_data_is_not_a_filler(self):
        cleaner = self.cleaner()
        for text in ('Что такое типа данных string?', 'объясни понятие типа данных',
                     'проверка ТИПА ДАННЫХ', 'сравнение типа данных int и string'):
            with self.subTest(text=text):
                self.assertEqual(cleaner.clean(text), text)
        self.assertEqual(cleaner.clean('ну типа покажи пример типа данных string'),
                         'покажи пример типа данных string')

    def test_removes_only_unambiguous_filler_tails(self):
        cases = [
            ('Эээ, ну короче, типа покажи мне, как написать класс на C#, ну ты понял',
             'покажи мне, как написать класс на C#'),
            ('объясни тип данных string, ну ты понял.', 'объясни тип данных string'),
            ('покажи прогноз ну вы поняли', 'покажи прогноз'),
            ('скажи «ну ты понял», ну ты понял', 'скажи «ну ты понял»'),
            ('ну ты понял', ''),
        ]
        cleaner = self.cleaner()
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(cleaner.clean(raw), expected)
                self.assertEqual(cleaner.clean(expected), expected)

    def test_meaningful_tail_words_and_quoted_corrections_are_preserved(self):
        cleaner = self.cleaner()
        for text in ('ты понял?', 'объясни что ты понял',
                     'скажи «ну ты понял»', 'переведи "ну вы поняли"',
                     'объясни выражение «хотя нет»',
                     'объясни выражение «ой нет, точнее»',
                     'напиши «типа данных»'):
            with self.subTest(text=text):
                self.assertEqual(cleaner.clean(text), text)

    def test_although_no_can_replace_a_command_without_forget(self):
        cleaner = self.cleaner()
        cases = [
            ('Открой браузер и найди погоду... хотя нет, забудь, открой блокнот',
             'открой блокнот'),
            ('открой браузер хотя нет открой блокнот', 'открой блокнот'),
            ('поставь таймер на семь а хотя нет на восемь', 'поставь таймер на восемь'),
            ('покажи погоду хотя нет', ''),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(cleaner.clean(raw), expected)


class FinalQueryTests(unittest.TestCase):
    def setUp(self):
        self.submitted = []
        received = self.submitted
        class Runtime:
            def submit(self, token, text):
                received.append(text)
                return True
            def cancel(self): pass
        self.controller = VoiceSessionController(Settings())
        self.controller.runtime = Runtime()
        self.controller.mic_state = 'listening'

    def tearDown(self):
        self.controller.runtime = None
        self.controller.shutdown()
        self.controller._poll()

    def phrase(self, text):
        now = perf_counter()
        return {'id': 'query', 'text': text, 'start': now - 1,
                'speech_end': now - .7, 'ready': now}

    def test_only_clean_query_reaches_runtime_and_logs_do_not_copy_conversation(self):
        raw = 'ну погода в Москве ой нет эээ погода в Бишкеке'
        phrase = self.phrase(raw)
        with self.assertLogs('alice_voice.controller', level='INFO') as logs:
            self.controller._phrase(0, phrase)
        self.assertEqual(self.submitted, ['погода в Бишкеке'])
        self.assertEqual(self.controller.recognized, 'погода в Бишкеке')
        self.assertEqual(phrase['text'], raw)
        self.assertNotIn(raw, '\n'.join(logs.output))
        self.assertNotIn('погода в Бишкеке', '\n'.join(logs.output))
        self.assertIn('raw_chars=', '\n'.join(logs.output))
        self.assertIn('clean_chars=', '\n'.join(logs.output))

    def test_filler_only_and_cancelled_queries_do_not_start_llm(self):
        for text in ('ну эээ ммм типа', 'покажи погоду ой нет', 'забудь'):
            with self.subTest(text=text):
                self.controller._phrase(0, self.phrase(text))
                self.assertEqual(self.submitted, [])
                self.assertIsNone(self.controller.guard.active)
                self.assertEqual(self.controller.stage, 'idle')

    def test_exact_stop_command_keeps_existing_behavior(self):
        self.controller._phrase(0, self.phrase('отключи микрофон'))
        self.assertEqual(self.submitted, [])
        self.assertEqual(self.controller.mic_state, 'off')

    def test_repaired_intent_reaches_llm(self):
        self.controller._phrase(0, self.phrase('ну поставь таймер на семь точнее на восемь'))
        self.assertEqual(self.submitted, ['поставь таймер на восемь'])

    def test_audit_regressions_reach_llm_as_complete_queries(self):
        self.controller._phrase(0, self.phrase(
            'Поставь таймер на семь... ой нет, точнее на восемь минут'))
        self.assertEqual(self.submitted, ['Поставь таймер на восемь минут'])

    def test_technical_type_query_reaches_llm_unchanged(self):
        self.controller._phrase(0, self.phrase('Что такое типа данных string?'))
        self.assertEqual(self.submitted, ['Что такое типа данных string?'])

    def test_filler_tail_does_not_reach_llm(self):
        self.controller._phrase(0, self.phrase(
            'Эээ, ну короче, типа покажи мне, как написать класс на C#, ну ты понял'))
        self.assertEqual(self.submitted, ['покажи мне, как написать класс на C#'])

    def test_filter_cannot_turn_an_ordinary_request_into_a_stop_command(self):
        self.controller._phrase(0, self.phrase('скажи слово точнее отключайся'))
        self.assertEqual(self.submitted, ['отключайся'])
        self.assertEqual(self.controller.mic_state, 'listening')
