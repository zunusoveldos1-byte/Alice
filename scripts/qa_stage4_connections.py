"""Read-only Gemini metadata probe; reports timings, never headers or keys."""
import asyncio
import json
from pathlib import Path
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alice_voice.config import Settings
from alice_voice.providers import LLMProvider
from alice_voice.secrets import load_gemini_key


async def main():
    result = {'generation_requests': 0, 'rows': []}
    provider = None
    try:
        settings = Settings.load('settings.json')
        if settings.provider != 'gemini':
            result['skipped'] = 'Selected provider is not Gemini'
        else:
            provider = LLMProvider(settings)
            headers = {'x-goog-api-key': load_gemini_key()}
            for _ in range(3):
                events = []
                async def trace(name, info):
                    events.append(name)  # Never retain info (may contain secrets).
                start = perf_counter()
                response = await provider.client.get(
                    f'https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}',
                    headers=headers, extensions={'trace': trace})
                result['rows'].append({'status': response.status_code,
                    'duration_ms': (perf_counter() - start) * 1000,
                    'tcp_connects': events.count('connection.connect_tcp.started'),
                    'tls_handshakes': events.count('connection.start_tls.started')})
    except Exception as error:
        result['error_type'] = type(error).__name__
    finally:
        if provider is not None:
            await provider.close()
    Path('docs/stage4-connections-results.json').write_text(
        json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    asyncio.run(main())
