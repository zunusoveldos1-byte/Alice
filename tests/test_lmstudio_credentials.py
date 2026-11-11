import unittest
from unittest.mock import Mock
from tests import test_secure_credentials as fixtures
from alice_voice import secrets
from alice_voice.config import Settings


class LMStudioCredentialTests(unittest.TestCase):
    setUp = fixtures.CredentialTests.setUp

    def test_token_replacement_verified_delete_is_idempotent_and_isolated(self):
        secrets.save_gemini_key('gemini-fixture')
        secrets.save_lmstudio_token('lm-fixture-first')
        self.assertEqual(secrets.load_lmstudio_token(), 'lm-fixture-first')
        secrets.save_lmstudio_token('lm-fixture-replaced')
        self.assertEqual(secrets.load_lmstudio_token(), 'lm-fixture-replaced')
        self.assertEqual(secrets.load_gemini_key(), 'gemini-fixture')
        secrets.delete_lmstudio_token()
        secrets.delete_lmstudio_token()
        with self.assertRaises(secrets.MissingCredentialError):
            secrets.load_lmstudio_token()
        self.assertNotIn('lmstudio_token', Settings().to_dict())
        self.assertNotIn('lm-fixture', repr(Settings().to_dict()))

    def test_store_failures_are_safe(self):
        self.native.CredWrite = Mock(side_effect=fixtures.NativeError(5))
        with self.assertRaises(secrets.CredentialError) as caught:
            secrets.save_lmstudio_token('fixture-private')
        self.assertNotIn('fixture-private', str(caught.exception))
        self.native.CredRead = Mock(side_effect=fixtures.NativeError(5))
        with self.assertRaises(secrets.CredentialError) as caught:
            secrets.load_lmstudio_token()
        self.assertNotIn('test-secret', str(caught.exception))

    def test_url_does_not_accept_embedded_credentials_and_model_id_is_preserved(self):
        for url in ('http://a:password@localhost:1234/v1', 'http://localhost:1234/v1?token=fixture',
                    'file:///v1', 'http://localhost:wrong/v1', 'http://localhost:1234'):
            with self.assertRaises(ValueError):
                Settings(lmstudio_url=url).validate()
        settings = Settings(provider='lmstudio', lmstudio_model='publisher/Exact-ID:Q4_K_M').validate()
        self.assertEqual(Settings.from_dict(settings.to_dict()), settings)
