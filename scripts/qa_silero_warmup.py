"""Measure real Silero first-request PCM, with no playback or outbound network.

Run baseline and warmup in separate fresh processes; both require cached weights.
This measures TTS only, not microphone/LLM or acoustic first sound.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import sys
from threading import Event
from time import perf_counter
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alice_voice.config import Settings
from alice_voice.silero_tts import SileroModelCache, SileroSynthesizer


async def measure(mode):
    cache = SileroModelCache()
    if not (cache.directory / 'v4_ru.pt').is_file():
        raise RuntimeError('Cached Silero weights are required; downloads are disabled.')
    def deny(*args, **kwargs):
        raise AssertionError('Outbound network disabled during warmup validation')
    text = 'Таймер установлен на восемь минут.'
    result = {'mode': mode, 'voice': 'xenia', 'cpu_threads': 2, 'text': text,
              'playback': False, 'llm_used': False, 'outbound_network_blocked': True}
    with ThreadPoolExecutor(max_workers=1) as executor:
        synth = SileroSynthesizer(Settings(synthesizer='silero', timeout_s=180), executor, cache=cache)
        with patch.object(socket.socket, 'connect', deny), patch.object(socket, 'create_connection', deny):
            if mode == 'warmup':
                start = perf_counter()
                await synth.warmup(Event())
                result['background_warmup_ms'] = (perf_counter() - start) * 1000
                start = perf_counter()
                await synth.warmup(Event())
                result['repeat_warmup_ms'] = (perf_counter() - start) * 1000
            rows = []
            for _ in range(3):
                start = perf_counter()
                pcm = await synth.synthesize(text, Event())
                rows.append({'request_to_pcm_ms': (perf_counter() - start) * 1000,
                             'audio_ms': len(pcm.data) / 48000 * 1000})
            result['requests'] = rows
            result['model_initializations'] = cache.initializations
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('baseline', 'warmup'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(measure(args.mode))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
