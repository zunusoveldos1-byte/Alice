"""Real Russian SAPI/Silero/Vosk pause sweep; all PCM stays in RAM."""
import json
import math
from pathlib import Path
import re
import sys
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import miniaudio
import numpy as np
from alice_voice.audio import AudioFrame
from alice_voice.config import Settings
from alice_voice.recognition import PhraseSegmenter, VoskRecognizer
from alice_voice.speech import sapi_synthesize
from alice_voice.vad import SileroVAD


def part(text):
    p = sapi_synthesize(text, 'Microsoft Irina Desktop - Russian', Event(), 20)
    raw = bytes(miniaudio.convert_frames(miniaudio.SampleFormat.SIGNED16, 1, p.sample_rate,
                                        p.data, miniaudio.SampleFormat.SIGNED16, 1, 16000))
    samples = np.frombuffer(raw, dtype='<i2').astype(np.int32)
    audible = np.flatnonzero(np.abs(samples) > 100)
    return raw[int(audible[0]) * 2:(int(audible[-1]) + 1) * 2]


def main():
    settings = Settings()
    vad, stt = SileroVAD(settings.vad_model), VoskRecognizer(settings.vosk_model)
    pairs = [('Поставь таймер', 'на восемь минут'), ('Найди погоду', 'в Бишкеке'),
             ('Напомни завтра', 'купить молоко'), ('Скажи пожалуйста', 'сколько будет два плюс два')]
    rows = []
    for left, right in pairs:
        a, b = part(left), part(right)
        for pause in (160, 256, 320, 384, 512, 600):
            raw = bytes(1024 * 10) + a + bytes(pause * 32) + b
            speech_end = len(raw) / 32000
            raw += bytes(1024 * 40)
            raw += bytes((-len(raw)) % 1024)
            frames = [raw[i:i + 1024] for i in range(0, len(raw), 1024)]
            vad.reset()
            probabilities = [vad(pcm, 16000) for pcm in frames]
            for policy in ('250', '300', '350', 'adaptive_250_350', '650_control'):
                ms = 650 if policy == '650_control' else (350 if policy.startswith('adaptive') else int(policy))
                seg = PhraseSegmenter(silence_ms=ms, min_speech_ms=settings.min_speech_ms)
                stt.reset()
                texts, ended, partial = [], [], ''
                for index, pcm in enumerate(frames):
                    if policy.startswith('adaptive'):
                        ms = 250 if re.search(r'\b(?:минут|бишкеке|молоко|два)\s*$', partial) else 350
                        seg.silence_frames = math.ceil(ms / 32)
                    update = seg.feed(AudioFrame(pcm, (index + 1) * .032), probabilities[index])
                    if update.started: stt.reset()
                    for selected in update.frames: partial = stt.feed(selected.pcm)
                    if update.finished:
                        texts.append(stt.finalize())
                        ended.append((index + 1) * .032)
                cut = any(when < speech_end - .064 for when in ended) or len(texts) != 1
                rows.append({'query': left + ' ' + right, 'pause_ms': pause, 'policy': policy,
                             'recognized_parts': texts, 'finalized_at_s': ended, 'cut_or_split': cut})
    aggregate = {policy: {'cases': sum(row['policy'] == policy for row in rows),
                          'cut_or_split': sum(row['policy'] == policy and row['cut_or_split'] for row in rows)}
                 for policy in ('250', '300', '350', 'adaptive_250_350', '650_control')}
    result = {'source': 'Russian SAPI -> real Silero/Vosk, inserted within-phrase pauses; no microphone',
              'aggregate': aggregate, 'rows': rows,
              'decision': 'Keep configured endpoint for correctness; fast candidates are experiments, not product defaults'}
    Path('docs/stage4-endpoint-results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(aggregate, ensure_ascii=False))


if __name__ == '__main__': main()
