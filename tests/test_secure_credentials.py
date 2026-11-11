"""Credential migration and provider validation with fake secrets only."""
import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
import types
import unittest
from unittest.mock import Mock, patch

import httpx

from alice_voice.config import Settings
from alice_voice import secrets
from alice_voice.providers import ElevenLabsTTSProvider, LLMProvider, ProviderError


class NativeError(Exception):
    def __init__(self, code):
        self.winerror = code
        super().__init__(code, 'test-secret must never escape')


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.stored, self.events = {}, []
        def write(item, flags):
            self.events.append('write')
            self.stored[item['TargetName']] = dict(item, CredentialBlob=item['CredentialBlob'].encode('utf-16-le'))
        def read(target, kind):
            self.events.append('read')
            if target not in self.stored: raise NativeError(1168)
            return self.stored[target]
        def delete(target, kind, flags=0):
            self.events.append('delete')
            if target not in self.stored: raise NativeError(1168)
            del self.stored[target]
        self.native = types.SimpleNamespace(CRED_TYPE_GENERIC=1, CRED_PERSIST_LOCAL_MACHINE=2,
                                            CredWrite=write, CredRead=read, CredDelete=delete)
        self.mock = patch.dict(sys.modules, {'win32cred': self.native})
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def legacy(self, directory, key='test-secret'):
        path = Path(directory) / 'settings.json'
        path.write_text(json.dumps(dict(Settings().to_dict(), elevenlabs_api_key=key)), encoding='utf-8')
        return path

    def test_migration_verifies_read_before_removing_plaintext(self):
        with TemporaryDirectory() as directory:
            path = self.legacy(directory)
            original_replace = __import__('os').replace
            def replace(source, destination):
                self.assertEqual(self.events, ['write', 'read'])
                self.assertEqual(json.loads(path.read_text())['elevenlabs_api_key'], 'test-secret')
                self.assertNotIn('test-secret', Path(source).read_text(encoding='utf-8'))
                original_replace(source, destination)
            with patch('alice_voice.config.os.replace', side_effect=replace):
                settings = Settings.load(path)
            self.assertNotIn('elevenlabs_api_key', settings.to_dict())
            self.assertNotIn('elevenlabs_api_key', json.loads(path.read_text(encoding='utf-8')))
            self.assertEqual(secrets.load_elevenlabs_key(), 'test-secret')
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_write_failure_preserves_original_without_new_backup(self):
        with TemporaryDirectory() as directory:
            path = self.legacy(directory)
            before = path.read_bytes()
            self.native.CredWrite = Mock(side_effect=NativeError(5))
            with self.assertRaisesRegex(RuntimeError, 'не измен') as caught:
                Settings.load(path)
            self.assertNotIn('test-secret', str(caught.exception))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_readback_failure_or_mismatch_preserves_plaintext(self):
        for failure in (NativeError(5), {'CredentialBlob': 'different-value'}):
            with self.subTest(failure=type(failure).__name__), TemporaryDirectory() as directory:
                path = self.legacy(directory)
                before = path.read_bytes()
                self.native.CredRead = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
                with self.assertRaises(RuntimeError) as caught:
                    Settings.load(path)
                self.assertEqual(path.read_bytes(), before)
                self.assertNotIn('test-secret', str(caught.exception))
                self.assertNotIn('different-value', str(caught.exception))

    def test_file_replace_failure_keeps_both_original_and_verified_credential(self):
        with TemporaryDirectory() as directory:
            path = self.legacy(directory)
            before = path.read_bytes()
            with patch('alice_voice.config.os.replace', side_effect=OSError('test-secret')):
                with self.assertRaises(RuntimeError) as caught:
                    Settings.load(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(secrets.load_elevenlabs_key(), 'test-secret')
            self.assertNotIn('test-secret', str(caught.exception))
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_empty_legacy_field_is_removed_without_touching_credentials(self):
        with TemporaryDirectory() as directory:
            path = self.legacy(directory, '')
            Settings.load(path)
            self.assertEqual(self.events, [])
            self.assertNotIn('elevenlabs_api_key', path.read_text(encoding='utf-8'))

    def test_invalid_settings_do_not_change_credential_or_file(self):
        with TemporaryDirectory() as directory:
            path = self.legacy(directory)
            data = json.loads(path.read_text())
            data['silence_ms'] = -1
            path.write_text(json.dumps(data), encoding='utf-8')
            before = path.read_bytes()
            with self.assertRaises(ValueError): Settings.load(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(self.events, [])

    def test_replacement_and_idempotent_deletion_use_separate_targets(self):
        secrets.save_gemini_key('gemini-fixture')
        secrets.save_elevenlabs_key('test-secret')
        secrets.save_elevenlabs_key('replacement-fixture')
        self.assertEqual(secrets.load_elevenlabs_key(), 'replacement-fixture')
        self.assertEqual(secrets.load_gemini_key(), 'gemini-fixture')
        secrets.delete_elevenlabs_key()
        secrets.delete_elevenlabs_key()
        with self.assertRaisesRegex(RuntimeError, 'ElevenLabs'):
            secrets.load_elevenlabs_key()
        self.assertEqual(secrets.load_gemini_key(), 'gemini-fixture')
        secrets.delete_gemini_key()
        with self.assertRaises(RuntimeError): secrets.load_gemini_key()

    def test_storage_and_delete_errors_are_safe_and_do_not_claim_success(self):
        self.native.CredRead = Mock(side_effect=NativeError(5))
        self.native.CredDelete = Mock(side_effect=NativeError(5))
        for action in (secrets.load_elevenlabs_key, secrets.delete_elevenlabs_key):
            with self.subTest(action=action.__name__):
                with self.assertRaises(RuntimeError) as caught: action()
                self.assertNotIn('test-secret', str(caught.exception))
                self.assertIn('Credential Manager', str(caught.exception))

    def test_settings_cannot_hold_or_copy_secrets(self):
        self.assertNotIn('elevenlabs_api_key', Settings().to_dict())
        with self.assertRaises((ValueError, TypeError)):
            Settings.from_dict({'elevenlabs_api_key': 'test-secret'})
        with self.assertRaises((ValueError, TypeError)):
            Settings(elevenlabs_api_key='test-secret')

    def test_ordinary_save_cannot_discard_an_unmigrated_key(self):
        with TemporaryDirectory() as directory:
            path = self.legacy(directory)
            before = path.read_bytes()
            with self.assertRaises(RuntimeError): Settings(volume=.4).save(path)
            self.assertEqual(path.read_bytes(), before)

    def test_migration_does_not_overwrite_a_file_changed_during_credential_write(self):
        with TemporaryDirectory() as directory:
            path = self.legacy(directory)
            write = self.native.CredWrite
            def concurrent_write(item, flags):
                write(item, flags)
                data = json.loads(path.read_text())
                data['elevenlabs_api_key'] = 'concurrent-fixture'
                path.write_text(json.dumps(data), encoding='utf-8')
            self.native.CredWrite = concurrent_write
            with self.assertRaises(RuntimeError): Settings.load(path)
            self.assertEqual(json.loads(path.read_text())['elevenlabs_api_key'], 'concurrent-fixture')


class ProviderConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_stops_runtime_before_llm_or_tts_http(self):
        from PyQt6.QtCore import QCoreApplication
        from alice_voice.metrics import LatencyMetrics
        from alice_voice.workers import DialogRuntime, WorkerSignals
        app = QCoreApplication.instance() or QCoreApplication([])
        signals, errors = WorkerSignals(), []
        signals.failed.connect(lambda token, error: errors.append(error))
        r = DialogRuntime(Settings(synthesizer='elevenlabs'), signals, LatencyMetrics())
        r.provider = Mock()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: self.fail('unexpected HTTP'))) as client:
            r.synth = ElevenLabsTTSProvider(r.settings, client=client, key_loader=lambda: '')
            await r._process(1, 'question', Event())
        r.provider.stream.assert_not_called()
        self.assertTrue(errors)
        self.assertIn('Ключ ElevenLabs отсутствует', errors[0])
        self.assertFalse(r.history)
        self.assertIsNone(r.stream_session)

    async def test_invalid_credential_header_is_never_sent_or_echoed(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: self.fail('unexpected HTTP'))) as client:
            p = ElevenLabsTTSProvider(Settings(elevenlabs_voice_id='voice'), client=client,
                                      key_loader=lambda: 'test-secret\r\nHeader: injection')
            with self.assertRaisesRegex(ProviderError, 'формат') as caught:
                await p.synthesize('Привет', Event())
            self.assertNotIn('test-secret', str(caught.exception))

    async def test_selected_ollama_model_is_requested_without_fallback(self):
        requested = []
        def handler(request):
            requested.append(json.loads(request.content)['model'])
            return httpx.Response(200, content=b'{"message":{"content":"Answer."}}\n')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            p = LLMProvider(Settings(ollama_model='my-installed-model:custom'), client)
            self.assertEqual(await p.generate([], lambda: None), 'Answer.')
        self.assertEqual(requested, ['my-installed-model:custom'])

    async def test_missing_elevenlabs_key_fails_before_any_http(self):
        requests = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: requests.append(r))) as client:
            loader = Mock(return_value='')
            p = ElevenLabsTTSProvider(Settings(synthesizer='elevenlabs', elevenlabs_voice_id='voice'),
                                      client=client, key_loader=loader)
            with self.assertRaisesRegex(ProviderError, '(?i)ключ ElevenLabs'):
                await p.synthesize('Привет', Event())
            self.assertEqual(requests, [])

    async def test_loader_error_is_not_exposed_or_replaced_by_another_provider(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: self.fail('unexpected HTTP'))) as client:
            p = ElevenLabsTTSProvider(Settings(synthesizer='elevenlabs', elevenlabs_voice_id='voice'),
                                      client=client, key_loader=Mock(side_effect=RuntimeError('test-secret')))
            with self.assertRaisesRegex(ProviderError, 'Credential Manager') as caught:
                await p.synthesize('Привет', Event())
            self.assertNotIn('test-secret', str(caught.exception))
            self.assertEqual(p.settings.synthesizer, 'elevenlabs')

    async def test_each_synthesis_reads_current_key_after_replacement(self):
        headers = []
        def handler(request):
            headers.append(request.headers['xi-api-key'])
            return httpx.Response(200, content=b'\x01\x00', headers={'content-type': 'audio/pcm'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            p = ElevenLabsTTSProvider(Settings(elevenlabs_voice_id='voice'), client=client,
                                      key_loader=Mock(side_effect=['first-fixture', 'second-fixture']))
            await p.synthesize('Первый', Event())
            await p.synthesize('Второй', Event())
        self.assertEqual(headers, ['first-fixture', 'second-fixture'])

    async def test_ollama_404_suggests_installed_model_without_echoing_body(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(404, json={'error': 'test-secret'}))) as client:
            p = LLMProvider(Settings(), client)
            with self.assertRaisesRegex(ProviderError, 'установленную модель') as caught:
                await p.generate([], lambda: None)
            self.assertIn('404', str(caught.exception))
            self.assertNotIn('test-secret', str(caught.exception))
            self.assertEqual(p.settings.provider, 'ollama')

    async def test_unreachable_ollama_is_distinct_from_missing_model(self):
        def handler(request): raise httpx.ConnectError('test-secret', request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(ProviderError, 'Сервер Ollama недоступен') as caught:
                await LLMProvider(Settings(), client).generate([], lambda: None)
            self.assertNotIn('test-secret', str(caught.exception))
            self.assertNotIn('404', str(caught.exception))

    async def test_ollama_timeout_and_server_error_are_separate_safe_messages(self):
        async def timeout(request): raise httpx.ReadTimeout('test-secret', request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            with self.assertRaisesRegex(ProviderError, 'Ollama.*таймаут'):
                await LLMProvider(Settings(), client).generate([], lambda: None)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503, text='test-secret'))) as client:
            with self.assertRaisesRegex(ProviderError, 'Сервер Ollama недоступен') as caught:
                await LLMProvider(Settings(), client).generate([], lambda: None)
            self.assertNotIn('test-secret', str(caught.exception))
