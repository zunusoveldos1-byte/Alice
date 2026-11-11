import types
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from alice_voice.recognition import SileroVAD
from alice_voice.secrets import save_gemini_key, load_gemini_key


class AdapterTests(unittest.TestCase):
    def test_silero_cpu_session_reused_and_states_isolated(self):
        import numpy as np
        sessions = []
        class Session:
            def __init__(self, path, sess_options, providers):
                self.calls = 0
                self.providers = providers
                sessions.append(self)
            def get_inputs(self):
                return [types.SimpleNamespace(name=n) for n in ('input', 'state', 'sr')]
            def run(self, names, inputs):
                self.calls += 1
                self_outer.assertEqual(inputs['input'].shape, (1, 576))
                self_outer.assertEqual(inputs['input'].dtype, np.float32)
                self_outer.assertEqual(int(inputs['sr']), 16000)
                self_outer.assertEqual(inputs['state'].shape, (2, 1, 128))
                return np.array([[.9]], dtype=np.float32), inputs['state'] + 1
        self_outer = self
        module = types.SimpleNamespace(SessionOptions=types.SimpleNamespace, InferenceSession=Session)
        with TemporaryDirectory() as directory, patch.dict(sys.modules, {'onnxruntime': module}):
            path = Path(directory) / 'fake.onnx'
            path.write_bytes(b'model')
            first, second = SileroVAD(path), SileroVAD(path)
            first.predict(bytes(1024)); first.predict(bytes(1024))
            second.predict(bytes(1024))
            self.assertEqual(float(first.state[0, 0, 0]), 2)
            self.assertEqual(float(second.state[0, 0, 0]), 1)
            first.reset()
            self.assertEqual(float(first.state[0, 0, 0]), 0)
            self.assertEqual(float(second.state[0, 0, 0]), 1)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].calls, 3)
        self.assertEqual(sessions[0].providers, ['CPUExecutionProvider'])

    def test_vosk_weights_reused_but_each_stream_has_a_fresh_recognizer(self):
        from unittest.mock import Mock
        from alice_voice.recognition import VoskRecognizer
        weights = Mock()
        module = types.SimpleNamespace(SetLogLevel=Mock(), Model=Mock(return_value=weights),
                                       KaldiRecognizer=Mock(side_effect=lambda *args: Mock()))
        with TemporaryDirectory() as directory, patch.dict(sys.modules, {'vosk': module}), \
             patch('alice_voice.recognition.vosk_path', return_value=directory):
            first = VoskRecognizer(directory)
            first.segments.append('old phrase')
            second = VoskRecognizer(directory)
            self.assertIs(first.model, second.model)
            self.assertIsNot(first.native, second.native)
            self.assertEqual(second.segments, [])
            second.reset()
            self.assertEqual(first.segments, ['old phrase'])
        module.Model.assert_called_once()
        self.assertEqual(module.KaldiRecognizer.call_count, 2)

    def test_credential_manager_utf16_and_no_json(self):
        written = []
        def write(credential, flags):
            # pywin32 CredWrite accepts Unicode and encodes it internally.
            if not isinstance(credential['CredentialBlob'], str):
                raise TypeError('Objects of type bytes can not be converted to Unicode.')
            stored = dict(credential)
            stored['CredentialBlob'] = credential['CredentialBlob'].encode('utf-16-le')
            written.append(stored)
        module = types.SimpleNamespace(
            CRED_TYPE_GENERIC=1, CRED_PERSIST_LOCAL_MACHINE=2,
            CredWrite=write,
            CredRead=lambda target, kind: written[-1],
        )
        with patch.dict(sys.modules, {'win32cred': module}):
            save_gemini_key('  AQ.fake-test-key  ')
            self.assertEqual(load_gemini_key(), 'AQ.fake-test-key')
        self.assertEqual(written[0]['CredentialBlob'], 'AQ.fake-test-key'.encode('utf-16-le'))

    def test_credential_error_is_safe(self):
        def fail(*args):
            raise RuntimeError('fake-test-key in native error')
        with patch.dict(sys.modules, {'win32cred': types.SimpleNamespace(CRED_TYPE_GENERIC=1, CredRead=fail)}):
            with self.assertRaises(RuntimeError) as error:
                load_gemini_key()
        self.assertNotIn('fake-test-key', str(error.exception))


if __name__ == '__main__':
    unittest.main()
