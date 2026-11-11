"""Incremental sentence boundaries, independent of Qt and network transports."""
import re


class SentenceChunker:
    _boundaries = re.compile(r'[.!?]+[»”\")\]]*')
    _protected = re.compile(r'\[[^\]]*(?:\]|$)(?:\([^)]*(?:\)|$))?|`[^`]*(?:`|$)|https?://\S+|www\.\S+')
    _abbreviations = {'руб', 'коп', 'т', 'д', 'е', 'г', 'ул', 'им', 'др', 'см', 'рис'}

    def __init__(self, early_fragments=False) -> None:
        self.buffer = ''
        self.early_fragments = early_fragments
        self._first_fragment = True

    def feed(self, delta: str) -> list[str]:
        self.buffer += delta
        sentences = []
        consumed = 0
        protected = [(match.start(), match.end()) for match in self._protected.finditer(self.buffer)]
        for match in self._boundaries.finditer(self.buffer):
            position = match.start()
            if any(left <= position < right for left, right in protected):
                continue
            if match.group() == '.':
                before = self.buffer[position - 1:position] if position else ''
                after = self.buffer[position + 1:position + 2]
                # A number-ending delta needs one character of lookahead.
                if before.isdigit() and (not after or after.isdigit()):
                    continue
                word = re.search(r'(\w+)$', self.buffer[:position])
                if word and word.group().casefold() in self._abbreviations:
                    continue
            sentence = self.buffer[consumed:match.end()].strip()
            if sentence:
                sentences.append(sentence)
            consumed = match.end()
        self.buffer = self.buffer[consumed:]
        if sentences:
            self._first_fragment = False
        if self.early_fragments and self._first_fragment and len(self.buffer) >= 24:
            protected = [(m.start(), m.end()) for m in self._protected.finditer(self.buffer)]
            for match in re.finditer(r'[,;:]\s+|\s+', self.buffer):
                cut = match.end()
                if cut < 24 or (cut < 56 and self.buffer[match.start()] not in ',;:'):
                    continue
                if any(left < cut < right for left, right in protected):
                    continue
                before, after = self.buffer[:cut].rstrip(), self.buffer[cut:]
                # Need a following token; never break grouped numbers or a
                # dangling preposition. No added punctuation or dropped words.
                if not after or (before[-1:].isdigit() and after[:1].isdigit()):
                    continue
                if before.split()[-1].casefold() in {'и', 'в', 'на', 'с', 'к', 'не', 'что', 'чтобы', 'или', 'но', 'а'}:
                    continue
                sentences.append(before)
                self.buffer = after
                self._first_fragment = False
                break
        return sentences

    def finish(self) -> list[str]:
        remaining, self.buffer = self.buffer.strip(), ''
        return [remaining] if remaining else []
