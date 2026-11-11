import unittest
from alice_voice.streaming import SentenceChunker, SpokenStream
from alice_voice.speech import clean_speech


class SpeechPhraseTests(unittest.TestCase):
    def test_long_unfinished_sentence_is_not_split_at_arbitrary_space(self):
        chunker = SentenceChunker(early_fragments=True)
        text = 'Это довольно длинное объяснение которое ещё не содержит законченной смысловой фразы'
        self.assertEqual(chunker.feed(text), [])
        self.assertEqual(chunker.feed(' и продолжается.'), [text + ' и продолжается.'])

    def test_dependent_clause_waits_for_main_clause(self):
        chunker = SentenceChunker(early_fragments=True)
        self.assertEqual(chunker.feed('Если завтра будет солнечная погода, мы'), [])
        self.assertEqual(chunker.feed(' пойдём гулять.'), ['Если завтра будет солнечная погода, мы пойдём гулять.'])

    def test_reasoning_code_json_and_markdown_across_every_split(self):
        raw = '<think>Скрытая мысль.</think>```json\n{"tool":"x"}\n``` **Привет!** {"tool":"x","args":{"q":"}"}} Второе предложение.'
        for split in range(1, len(raw)):
            filt = SpokenStream()
            text = filt.feed(raw[:split]) + filt.feed(raw[split:])
            self.assertEqual(clean_speech(text), 'Привет! Второе предложение.', split)

    def test_numbers_and_abbreviations_keep_order_without_loss(self):
        text = 'Цена 1 234,50 руб. и 3.14 единицы. На ул. Ленина в 12:30. Готово!'
        chunker = SentenceChunker(early_fragments=True)
        chunks = []
        for char in text:
            chunks.extend(chunker.feed(char))
        chunks.extend(chunker.finish())
        self.assertEqual(' '.join(chunks), text)
        self.assertTrue(any('1 234,50' in chunk for chunk in chunks))

    def test_short_ready_answer_is_not_held_for_another_sentence(self):
        self.assertEqual(SentenceChunker().feed('Да!'), ['Да!'])
