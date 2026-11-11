from collections import deque


class SessionGuard:
    """Main-thread dialog generation and bounded deduplication ledger."""
    def __init__(self, capacity=256):
        self.generation = 0
        self.active = None
        self.seen = deque(maxlen=capacity)

    def begin(self, utterance_id):
        if utterance_id in self.seen:
            return None
        self.seen.append(utterance_id)
        if self.active is not None:
            return None
        self.generation += 1
        self.active = self.generation
        return self.active

    def valid(self, token):
        return token is not None and self.active == token

    def cancel(self):
        self.generation += 1
        self.active = None

    def finish(self, token):
        if self.valid(token):
            self.active = None
