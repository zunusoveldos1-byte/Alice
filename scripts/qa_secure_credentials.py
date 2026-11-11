"""Opt-in real Credential Manager checks with disposable non-API values.

Uses a unique target and removes it in finally. Never reads user credentials
or emits credential contents. No network requests, backups or audio.
"""
import argparse
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alice_voice.config import Settings
from alice_voice import secrets as vault


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-credentials', action='store_true', required=True)
    parser.parse_args()
    target = 'AliceVoice/QA/ElevenLabs/' + uuid4().hex
    original = 'alice-test-value-' + uuid4().hex
    replacement = 'alice-test-value-' + uuid4().hex
    result = {'native_credential_manager': True, 'uses_actual_user_targets': False,
              'secret_values_in_report': False, 'write_read': False, 'replacement': False,
              'deletion': False, 'repeat_deletion': False, 'migration': False,
              'json_has_no_key': False, 'no_backup_created': False, 'cleanup': False}
    with patch.object(vault, 'ELEVENLABS_TARGET', target):
        try:
            vault.save_elevenlabs_key(original)
            result['write_read'] = vault.load_elevenlabs_key() == original
            vault.save_elevenlabs_key(replacement)
            result['replacement'] = vault.load_elevenlabs_key() == replacement
            vault.delete_elevenlabs_key()
            try:
                vault.load_elevenlabs_key()
            except vault.MissingCredentialError:
                result['deletion'] = True
            vault.delete_elevenlabs_key()
            result['repeat_deletion'] = True
            with TemporaryDirectory(prefix='stage3-native-', dir='.build-tmp') as directory:
                path = Path(directory) / 'settings.json'
                path.write_text(json.dumps(dict(Settings().to_dict(), elevenlabs_api_key=original)), encoding='utf-8')
                loaded = Settings.load(path)
                result['migration'] = vault.load_elevenlabs_key() == original
                data = path.read_text(encoding='utf-8')
                result['json_has_no_key'] = 'elevenlabs_api_key' not in loaded.to_dict() and original not in data
                result['no_backup_created'] = list(Path(directory).iterdir()) == [path]
        except Exception as error:
            result['error_type'] = type(error).__name__  # Native messages may contain secrets.
        finally:
            try:
                vault.delete_elevenlabs_key()
                result['cleanup'] = True
            except Exception as error:
                result['cleanup_error_type'] = type(error).__name__
    Path('docs/stage3-native-credential-results.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    checks = ('write_read', 'replacement', 'deletion', 'repeat_deletion', 'migration',
              'json_has_no_key', 'no_backup_created', 'cleanup')
    if not all(result[name] for name in checks):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
