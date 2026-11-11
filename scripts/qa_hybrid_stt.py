"""Native synthetic comparison. PCM stays in RAM; no microphone or playback."""
import json
from pathlib import Path
import re
import sys
from threading import Event
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import miniaudio
from alice_voice.config import Settings
from alice_voice.recognition import VoskRecognizer
from alice_voice.speech import sapi_synthesize
from alice_voice.whisper_stt import WhisperRefiner


def words(text):
    return re.findall(r'\w+', text.casefold().replace('ё', 'е'))


def wer(expected, actual):
    reference, heard = words(expected), words(actual)
    row = list(range(len(heard) + 1))
    for i, word in enumerate(reference, 1):
        next_row = [i]
        for j, other in enumerate(heard, 1):
            next_row.append(min(row[j] + 1, next_row[-1] + 1, row[j - 1] + (word != other)))
        row = next_row
    return row[-1] / max(1, len(reference))


def main():
    settings = Settings()
    vosk = VoskRecognizer(settings.vosk_model)
    started = perf_counter()
    whisper = WhisperRefiner(settings.whisper_model)
    cold = perf_counter() - started
    rows = []
    for phrase in ('Найди погоду в Бишкеке', 'Открой блокнот',
                   'Поставь таймер на восемь минут', 'Расскажи про Кыргызстан'):
        speech = sapi_synthesize(phrase, 'Microsoft Irina Desktop - Russian', Event(), 20)
        data = bytes(miniaudio.convert_frames(miniaudio.SampleFormat.SIGNED16, 1,
                     speech.sample_rate, speech.data, miniaudio.SampleFormat.SIGNED16, 1, 16000))
        data = bytes(3200) + data + bytes(22400)
        data += bytes((-len(data)) % 1024)
        vosk.reset(); started = perf_counter()
        for offset in range(0, len(data), 1024): vosk.feed(data[offset:offset + 1024])
        raw = vosk.finalize(); vosk_ms = (perf_counter() - started) * 1000
        started = perf_counter(); refined = whisper.refine(data)
        row = {'expected': phrase, 'vosk': raw, 'whisper': refined,
               'vosk_wer': wer(phrase, raw), 'whisper_wer': wer(phrase, refined),
               'vosk_ms': vosk_ms, 'whisper_ms': (perf_counter() - started) * 1000,
               'audio_ms': len(data) / 32}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=True), flush=True)
    result = {'method': 'SAPI synthetic PCM, real Vosk and faster-whisper small CPU int8',
              'live_microphone': False, 'audio_saved': False, 'playback': False,
              'cold_whisper_ms': cold * 1000, 'rows': rows}
    Path('docs/hybrid-stt-native-results.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__': main()
