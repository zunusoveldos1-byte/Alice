"""Install only the pinned Windows RNNoise native asset, without CLI/AV dependencies."""
import hashlib
from pathlib import Path
import subprocess
import sys
from urllib.request import urlopen
from tempfile import TemporaryDirectory
from zipfile import ZipFile

ROOT=Path(__file__).resolve().parents[1]
SHA256='f46015e5a58212d2883bf8e5a2cafb3c6b115176d8039f8340132cb24866b7da'

if __name__=='__main__':
    if sys.platform!='win32':raise SystemExit('This helper prepares the Windows x64 native asset only.')
    target=ROOT/'models'/'rnnoise';target.mkdir(parents=True,exist_ok=True)
    with TemporaryDirectory() as temp:
        subprocess.run([sys.executable,'-m','pip','download','--no-deps','--only-binary=:all:',
                        '--dest',temp,'pyrnnoise==0.4.5'],check=True)
        wheel=Path(temp)/'pyrnnoise-0.4.5-py3-none-win_amd64.whl'
        if hashlib.sha256(wheel.read_bytes()).hexdigest()!=SHA256:raise SystemExit('RNNoise wheel SHA-256 mismatch')
        with ZipFile(wheel) as archive:
            dlls=[name for name in archive.namelist() if name.endswith('/pyrnnoise/rnnoise.dll') or name=='pyrnnoise/rnnoise.dll']
            if len(dlls)!=1:raise SystemExit('Expected exactly one RNNoise native asset')
            (target/'rnnoise.dll').write_bytes(archive.read(dlls[0]))
            (target/'LICENSE-pyrnnoise.txt').write_bytes(archive.read('pyrnnoise-0.4.5.dist-info/licenses/LICENSE'))
    with urlopen('https://raw.githubusercontent.com/xiph/rnnoise/master/COPYING',timeout=20) as response:
        (target/'COPYING-RNNoise.txt').write_bytes(response.read(65536))
    print('RNNoise prepared:',target)
