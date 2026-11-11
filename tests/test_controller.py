import unittest
from alice_voice.session import SessionGuard


class SessionTests(unittest.TestCase):
    def test_dedup_and_one_active(self):
        guard = SessionGuard()
        token = guard.begin('a')
        self.assertIsNotNone(token)
        self.assertIsNone(guard.begin('b'))
        guard.finish(token)
        self.assertIsNone(guard.begin('a'))
        self.assertIsNone(guard.begin('b'))  # discarded while busy, never replayed
        self.assertIsNotNone(guard.begin('c'))

    def test_late_results_and_cancel(self):
        guard = SessionGuard()
        old = guard.begin('a')
        guard.cancel()
        new = guard.begin('b')
        self.assertFalse(guard.valid(old))
        self.assertTrue(guard.valid(new))
        guard.finish(old)
        self.assertTrue(guard.valid(new))

    def test_seen_ids_bounded(self):
        guard = SessionGuard(capacity=3)
        for i in range(10):
            token = guard.begin(str(i))
            guard.finish(token)
        self.assertEqual(len(guard.seen), 3)


if __name__ == '__main__':
    unittest.main()
