"""Measure real VAD/Vosk startup across capture restarts, without opening audio."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from time import perf_counter
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt6.QtCore import QCoreApplication
from alice_voice.config import Settings
from alice_voice.metrics import LatencyMetrics
from alice_voice.model_cache import LocalModelCache
from alice_voice.workers import CaptureWorker, WorkerSignals


class CountingCache(LocalModelCache):
    def __init__(self):
        super().__init__()
        self.loads = 0

    def get(self, path, loader):
        def counted():
            self.loads += 1
            return loader()
        return super().get(path, counted)


def measure(mode):
    app = QCoreApplication.instance() or QCoreApplication([])
    settings = replace(Settings.load('settings.json'), noise_mode='off', fair_mode=False, owner_only=False)
    result = {'mode': mode, 'real_models': True, 'microphone_opened': False,
              'noise_processing': False, 'llm_used': False, 'rows': []}
    vad_cache, stt_cache = CountingCache(), CountingCache()
    for index in range(4):
        if mode == 'baseline':
            vad_cache, stt_cache = CountingCache(), CountingCache()
        before = (vad_cache.loads, stt_cache.loads)
        signals, metrics = WorkerSignals(), LatencyMetrics()
        errors = []
        signals.error.connect(lambda epoch, message: errors.append(message))
        opened = []
        class NoAudio:
            def __init__(self, inbox, device): pass
            def open(self):
                opened.append(perf_counter())
                worker.stop()
            def close(self): pass
        worker = CaptureWorker(settings, signals, metrics, index, capture_factory=NoAudio)
        with patch('alice_voice.vad._MODEL_CACHE', vad_cache), \
             patch('alice_voice.recognition._MODEL_CACHE', stt_cache):
            start = perf_counter()
            worker.run()
        if errors or not opened:
            raise RuntimeError('Capture startup failed: ' + '; '.join(errors))
        stages = metrics.summary()
        result['rows'].append({'cycle': index + 1, 'to_capture_open_ms': (opened[0] - start) * 1000,
                               'vad_ms': stages['cold_vad']['mean_ms'],
                               'stt_ms': stages['cold_stt']['mean_ms'],
                               'vad_loads': vad_cache.loads - before[0],
                               'vosk_loads': stt_cache.loads - before[1]})
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('baseline', 'cached'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = measure(args.mode)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))
