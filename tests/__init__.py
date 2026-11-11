"""Isolate default memory paths, including Qt subprocess tests, from owner data."""
import atexit
import os
from pathlib import Path
from tempfile import TemporaryDirectory

root = Path(__file__).resolve().parents[1] / '.build-tmp'
root.mkdir(exist_ok=True)
session = os.environ.get('ALICE_VOICE_TEST_SESSION_DIR')
inherited = Path(session).resolve() if session else None
if (inherited is None or not inherited.is_dir() or inherited.parent != root.resolve()
        or not inherited.name.startswith('test-owner-')):
    _data = TemporaryDirectory(prefix='test-owner-', dir=root, ignore_cleanup_errors=True)
    inherited = Path(_data.name).resolve()
    os.environ['ALICE_VOICE_TEST_SESSION_DIR'] = str(inherited)
    atexit.register(_data.cleanup)
os.environ['ALICE_VOICE_DATA_DIR'] = str(inherited)
