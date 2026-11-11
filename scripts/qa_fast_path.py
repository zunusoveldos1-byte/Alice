"""Real warmed Silero fast replies: no LLM, audio device, recordings or network."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import socket
import sys
from threading import Event
from time import perf_counter
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PyQt6.QtCore import QCoreApplication
from alice_voice.config import Settings
from alice_voice.context_tools import ToolRouter
from alice_voice.metrics import LatencyMetrics
from alice_voice.silero_tts import SileroModelCache,SileroSynthesizer
from alice_voice.workers import DialogRuntime,WorkerSignals


async def measure():
    app=QCoreApplication.instance() or QCoreApplication([])
    settings=replace(Settings.load('settings.json'),synthesizer='silero',timeout_s=180)
    cache=SileroModelCache()
    if not (cache.directory/'v4_ru.pt').is_file():
        raise RuntimeError('Cached Silero weights are required; no downloads allowed.')
    def deny(*args,**kwargs): raise AssertionError('Network disabled')
    result={'real_silero':True,'voice':settings.silero_voice,'playback':False,'network_blocked':True,'rows':[]}
    with ThreadPoolExecutor(max_workers=1) as executor, \
         patch.object(socket.socket,'connect',deny), patch.object(socket,'create_connection',deny):
        synth=SileroSynthesizer(settings,executor,cache=cache)
        started=perf_counter()
        await synth.warmup(Event())
        result['warmup_ms']=(perf_counter()-started)*1000
        for token,phrase in enumerate(('привет','спасибо','как дела'),1):
            r=DialogRuntime(settings,WorkerSignals(),LatencyMetrics())
            r.synth=synth
            r.provider=Mock()
            backend=Mock(); backend.active_window.side_effect=AssertionError('Fast reply queried desktop')
            r.router=ToolRouter(settings,r.provider,desktop=backend,conversation=r.conversation)
            packets=[]; errors=[]
            started=perf_counter()
            def packet(turn,pcm,ready,sequence):
                packets.append({'ms':(perf_counter()-started)*1000,'audio_bytes':len(pcm.data)})
                r._release_pcm(turn,sequence)
            r.signals.pcm.connect(packet)
            r.signals.failed.connect(lambda *args:errors.append(args))
            await asyncio.wait_for(r._process(token,phrase,Event()),30)
            if errors or not packets: raise RuntimeError('Fast reply failed')
            r.provider.stream.assert_not_called()
            backend.active_window.assert_not_called()
            result['rows'].append({'phrase':phrase,'reply':r.history[-1]['content'],
                                   'first_pcm_ms':packets[0]['ms'],'llm_generation_calls':0,
                                   'desktop_queries':0,'pcm_packets':len(packets)})
    return result


if __name__=='__main__':
    result=asyncio.run(measure())
    Path('docs/fast-path-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2))
