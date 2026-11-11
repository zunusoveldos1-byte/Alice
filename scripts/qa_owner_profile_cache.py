"""Isolate profile lookup cost with synthetic vectors and a temporary owner file.

No real owner profile, recordings, speaker model, microphone or network is used.
Baseline reproduces the previous uncached load implementation.
"""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import perf_counter,process_time
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from alice_voice.owner_voice import OwnerProfile,unit


class UncachedProfile(OwnerProfile):
    def load(self):
        try:
            if self.path.stat().st_size>65536: return None
            data=json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('version')!=1 or data.get('samples')!=3 or not isinstance(data.get('model'),str): return None
            return data['model'],unit(data['vector'])
        except (OSError,ValueError,TypeError,KeyError,AttributeError):
            return None


def measure(profile,vector,count):
    original=Path.read_text
    reads=[]
    def read(path,*args,**kwargs):
        reads.append(path)
        return original(path,*args,**kwargs)
    with patch.object(Path,'read_text',read):
        start,cpu=perf_counter(),process_time()
        accepted=sum(profile.matches(vector,'synthetic-model',.65) for _ in range(count))
        elapsed,used=perf_counter()-start,process_time()-cpu
    assert accepted==count
    return {'lookups':count,'accepted':accepted,'file_content_reads':len(reads),
            'wall_ms':elapsed*1000,'process_cpu_ms':used*1000}


def main():
    vector=[1.]+[0.]*191
    with TemporaryDirectory() as directory:
        path=Path(directory)/'owner.json'
        OwnerProfile(path).enroll([vector]*3,'synthetic-model')
        result={'real_owner_profile_used':False,'speaker_inference_measured':False,
                'metadata_is_checked_each_lookup':True,
                'thirty_second_phrase_29_windows':{
                    'before':measure(UncachedProfile(path),vector,29),
                    'after':measure(OwnerProfile(path),vector,29)},
                'repeated_lookup':{'before':measure(UncachedProfile(path),vector,1000),
                                   'after':measure(OwnerProfile(path),vector,1000)}}
    Path('docs/owner-profile-cache-results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__': main()
