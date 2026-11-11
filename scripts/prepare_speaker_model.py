"""Explicit one-time download from the official sherpa-onnx model release."""
import hashlib
from pathlib import Path
import os
import sys
from tempfile import NamedTemporaryFile
import httpx
from concurrent.futures import ThreadPoolExecutor

URL = ('https://github.com/k2-fsa/sherpa-onnx/releases/download/'
       'speaker-recongition-models/wespeaker_en_voxceleb_resnet34.onnx')
SIZE = 26534365
SHA256 = '5ef208a9da1453335308a6b6f4e6dfbd7e183a38b604de0a57664f45d257fe94'


def main():
    target = Path(__file__).resolve().parents[1] / 'models' / 'speaker-recognition.onnx'
    if target.exists():
        if target.stat().st_size != SIZE or hashlib.sha256(target.read_bytes()).hexdigest() != SHA256:
            raise RuntimeError('Existing model failed integrity check; remove it explicitly before retrying')
        print('Model already exists:', target)
        print('sha256:', hashlib.sha256(target.read_bytes()).hexdigest())
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        digest = hashlib.sha256()
        size = 0
        with NamedTemporaryFile(dir=target.parent, suffix='.part', delete=False) as file:
            temporary = Path(file.name)
            with httpx.Client(trust_env=False, follow_redirects=True, timeout=30) as client:
                # Small bounded ranges work around networks stalling large asset responses.
                def fetch(start):
                    end = min(SIZE - 1, start + 65536 - 1)
                    response = client.get(URL, headers={'Range': f'bytes={start}-{end}'})
                    response.raise_for_status()
                    if (response.status_code != 206 or response.headers.get('content-range') != f'bytes {start}-{end}/{SIZE}'
                            or len(response.content) != end - start + 1):
                        raise RuntimeError('Unexpected model range')
                    return response.content
                with ThreadPoolExecutor(max_workers=4) as pool:
                    for chunk in pool.map(fetch, range(0, SIZE, 65536)):
                        size += len(chunk)
                        file.write(chunk)
                        digest.update(chunk)
                        if size % (1024 * 1024) == 0 or size == SIZE:
                            print('Downloaded', size, '/', SIZE, 'bytes', flush=True)
            if size != SIZE:
                raise RuntimeError('Incomplete model download')
            if digest.hexdigest() != SHA256:
                raise RuntimeError('Model checksum mismatch')
        os.replace(temporary, target)
        print('sha256:', digest.hexdigest(), flush=True)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
