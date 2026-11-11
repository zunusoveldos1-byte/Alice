"""Deterministic Russian disfluency cleanup for a finalized STT utterance."""
import re


# Quoted examples are literal content, not instructions to edit the query.
_QUOTE = r'«[^»]*»|“[^”]*”|„[^“]*“|"[^"\n]*"|\'[^\'\n]*\''
_CORRECTION = re.compile(
    rf'(?P<quote>{_QUOTE})|(?<![\w-])'
    r'(?P<marker>ой[\s,;:—-]+нет|точнее|'
    r'(?:а[\s,;:—-]+)?хотя[\s,;:—-]+нет|а[\s,;:—-]+хотя|забудь)(?![\w-])',
    re.IGNORECASE,
)
# Match before single fillers are stripped, and only at the utterance end.
# Questions and quoted examples remain meaningful content.
_FILLER_TAIL = re.compile(
    rf'(?P<quote>{_QUOTE})|[\s,;:]*'
    r'(?<![\w-])ну[\s,;:]+(?:ты\s+понял|вы\s+поняли)(?![\w-])[\s.!…]*$',
    re.IGNORECASE,
)
_FILLER = re.compile(
    rf'(?P<quote>{_QUOTE})|[\s,;:]*'
    r'(?<![\w-])(?P<word>э+|э(?:[-–]э)+|м{2,}|ну|короче|'
    r'как\s+бы(?!\s+(?:ты|вы|я|мы|он|она|они|оно)\b)|типа)(?![\w-])'
    r'(?:\s*(?:[,;:]|\.{3}|…))?',
    re.IGNORECASE,
)
_PREPOSITION = re.compile(
    rf'(?P<quote>{_QUOTE})|\b(?P<prep>на|в|во|к|ко|с|со|до|после|перед|через|про|о|об|для|из|по|у|от|за)\s+',
    re.IGNORECASE)
_NUMBER_WORD = (
    r'ноль|нуль|один|одна|одно|два|две|три|четыре|пять|шесть|семь|восемь|девять|'
    r'десять|одиннадцать|двенадцать|тринадцать|четырнадцать|пятнадцать|шестнадцать|'
    r'семнадцать|восемнадцать|девятнадцать|двадцать|тридцать|сорок|пятьдесят|'
    r'шестьдесят|семьдесят|восемьдесят|девяносто|сто|двести|триста|четыреста|'
    r'пятьсот|шестьсот|семьсот|восемьсот|девятьсот|тысяча|тысячи|тысяч')
_NUMBER = re.compile(
    rf'(?P<quote>{_QUOTE})|(?<!\w)(?P<value>\d+(?:[.,:/-]\d+)*|'
    rf'(?:{_NUMBER_WORD})(?:\s+(?:{_NUMBER_WORD}))*)(?!\w)', re.IGNORECASE)
_DAY = re.compile(
    rf'(?P<quote>{_QUOTE})|\b(?P<value>сегодня|завтра|послезавтра|вчера|'
    r'понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)\b', re.IGNORECASE)
_DATE = re.compile(rf'(?P<quote>{_QUOTE})|(?<![\w.])(?P<value>\d{{1,2}}\.\d{{1,2}}\.\d{{2,4}})(?![\w.])')
_TIME = re.compile(rf'(?P<quote>{_QUOTE})|(?<![\w:])(?P<value>\d{{1,2}}:\d{{2}})(?![\w:])')
_ACTION = re.compile(
    r'\b(?:поставь|установи|заведи|напомни|расскажи|покажи|объясни|скажи|'
    r'включи|включай|выключи|выключай|отключайся|открой|закрой|посмотри|найди|'
    r'запусти|останови|добавь|удали|создай|сделай|переведи)\b', re.IGNORECASE)


def _strip_fillers(text: str) -> str:
    text = _FILLER_TAIL.sub(lambda match: match.group('quote') or '', text)

    def replace(match):
        if match.group('quote') is not None:
            return match.group('quote')
        word = match.group('word').casefold()
        before = text[:match.start('word')]
        # These words also carry meaning in technical and comparative requests.
        if word == 'типа' and (
                re.match(r'\s+данных\b', text[match.end('word'):], re.IGNORECASE) or re.search(
                r'\b(?:данные|данных|значение|значения|объект|объекты|переменная|'
                r'переменные|файл|файлы|какого|такого|этого|любого|слово|слова)\s+$', before, re.IGNORECASE)):
            return match.group()
        if word == 'короче' and re.search(
                r'\b(?:ответ|текст|сообщение|фразу|строку|путь|маршрут|сделай|'
                r'намного|гораздо|чуть|немного)\s+$', before, re.IGNORECASE):
            return match.group()
        return ' '
    text = ' '.join(_FILLER.sub(replace, text).split())
    text = re.sub(r'\s+([,.;:!?])', r'\1', text)
    text = text.lstrip(' ,;:.!?—-…').rstrip(' ,;:—-')
    return text if re.search(r'\w', text) else ''


def _repair(previous: str, correction: str) -> str:
    """Merge only recognizable replacement slots; otherwise use the new thought."""
    if not previous or not correction or _ACTION.search(correction):
        return correction
    prep = _PREPOSITION.match(correction)
    if prep and prep.group('prep'):
        candidates = [match for match in _PREPOSITION.finditer(previous)
                      if match.group('prep') and match.group('prep').casefold() == prep.group('prep').casefold()]
        if candidates:
            return previous[:candidates[-1].start()] + correction
    for pattern in (_DAY, _DATE, _TIME, _NUMBER):
        value = pattern.match(correction)
        if value is None or value.group('value') is None:
            continue
        candidates = [match for match in pattern.finditer(previous) if match.group('value') is not None]
        if candidates:
            old = candidates[-1]
            # A bare replacement keeps units or the remaining reminder text.
            suffix = previous[old.end():] if value.end() == len(correction) else ''
            return previous[:old.start()] + correction + suffix
        # Never replace a time with a date (or the reverse) by falling back
        # to the broader numeric rule when a typed slot is absent.
        if pattern in (_DATE, _TIME):
            return correction
    old_object = re.match(r'((?:поставь|установи|заведи)\s+)(?:таймер|будильник)\b', previous, re.IGNORECASE)
    if old_object and re.match(r'(?:таймер|будильник)\b', correction, re.IGNORECASE):
        return old_object.group(1) + correction
    return correction


class DisfluencyFilter:
    """Repair explicit corrections and remove standalone filler words.

    Each call handles one complete utterance and carries no state across
    phrases. Adjacent repair markers keep the previous intent until a
    replacement arrives; "forget" resets it even without a replacement.
    A trailing correction cancels the utterance. Recognizable
    prepositional, numeric, date and timer/alarm slots retain the request.
    A new full command or an ambiguous correction replaces the old thought.
    This is a heuristic, not a general-purpose semantic parser. Quoted spans,
    "не забудь" reminders and "как бы ты..." questions are preserved.
    """

    def clean(self, text: str) -> str:
        markers = []
        for match in _CORRECTION.finditer(text):
            marker = match.group('marker')
            if marker is None:
                continue
            if marker.casefold() == 'забудь' and re.search(
                    r'\bне\s*$', text[:match.start()], re.IGNORECASE):
                continue
            markers.append(match)
        if not markers:
            return _strip_fillers(text)
        result = _strip_fillers(text[:markers[0].start()])
        for index, marker in enumerate(markers):
            end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
            correction = _strip_fillers(text[marker.end():end])
            # Adjacent repair markers share the same previous intent. A final
            # empty correction still cancels; "forget" always resets it.
            if (not correction and index + 1 < len(markers)
                    and marker.group('marker').casefold() != 'забудь'):
                continue
            result = correction if marker.group('marker').casefold() == 'забудь' else _repair(result, correction)
        return _strip_fillers(result)
