"""Exact small-talk replies. Compound requests always keep the normal route."""
import re

FAST_REPLIES = {
    'привет': 'Привет, я слушаю.',
    'здравствуй': 'Здравствуй, я слушаю.',
    'здравствуйте': 'Здравствуйте, я вас слушаю.',
    'доброе утро': 'Доброе утро, я слушаю.',
    'добрый день': 'Добрый день, я слушаю.',
    'добрый вечер': 'Добрый вечер, я слушаю.',
    'как дела': 'Всё хорошо, я готова помочь.',
    'спасибо': 'Пожалуйста, обращайся.',
    'большое спасибо': 'Пожалуйста, обращайся.',
    'благодарю': 'Пожалуйста, обращайся.',
}


def fast_reply(text: str) -> str | None:
    normalized = re.sub(r'\s+', ' ', text.casefold().replace('ё', 'е')).strip()
    return FAST_REPLIES.get(normalized.rstrip('.!?…').rstrip())
