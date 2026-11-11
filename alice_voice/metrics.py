from collections import defaultdict, deque
import logging
from threading import Lock

LIMITS = {'audio_queue': .2, 'vad': .032, 'stt_feed': .032, 'stt_finalize': .5, 'llm_first': 10,
          'llm_full': 45, 'tts': 20, 'pcm_to_playback': .5, 'endpoint_wait': 3}


class LatencyMetrics:
    def __init__(self, capacity=512):
        self.values = defaultdict(lambda: deque(maxlen=capacity))
        self.lock = Lock()

    def record(self, stage, seconds):
        with self.lock:
            self.values[stage].append(max(0, seconds))
        if seconds > LIMITS.get(stage, 120):
            logging.getLogger('alice_voice.latency').warning('stage=%s duration_ms=%.1f', stage, seconds * 1000)

    def summary(self):
        with self.lock:
            snapshot = {k: sorted(v) for k, v in self.values.items() if v}
        def percentile(values, fraction):
            position = (len(values) - 1) * fraction
            lower = int(position)
            upper = min(lower + 1, len(values) - 1)
            return (values[lower] + (values[upper] - values[lower]) * (position - lower)) * 1000
        return {k: {'count': len(v), 'mean_ms': sum(v) / len(v) * 1000,
                    'p50_ms': percentile(v, .5), 'p95_ms': percentile(v, .95)}
                for k, v in snapshot.items()}
