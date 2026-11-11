"""Synthetic local comparison only. No microphone, playback, human recordings or tools."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sys
from threading import Event
from time import perf_counter

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import numpy as np
import soxr
from alice_voice.audio import AudioFrame
from alice_voice.config import Settings
from alice_voice.fair_mode import ActivationGate
from alice_voice.noise import MicrophoneProcessor
from alice_voice.recognition import VoskRecognizer
from alice_voice.silero_tts import SileroSynthesizer


def recognize(stt,data):
    stt.reset()
    for i in range(0,len(data)-511,512):stt.feed(data[i:i+512].astype('<i2').tobytes())
    return stt.finalize()


def distance(reference,text):
    a=reference.split();b=text.split();row=list(range(len(b)+1))
    for i,x in enumerate(a,1):
        next_row=[i]
        for j,y in enumerate(b,1):next_row.append(min(row[j]+1,next_row[-1]+1,row[j-1]+(x!=y)))
        row=next_row
    return row[-1]/max(1,len(a))


async def run():
    settings=replace(Settings.load(ROOT/'settings.json'),fair_mode=True)
    reference='алиса открой блокнот'
    with ThreadPoolExecutor(max_workers=1) as executor:
        pcm=await SileroSynthesizer(settings,executor).synthesize(reference,Event())
        other=await SileroSynthesizer(replace(settings,silero_voice='aidar'),executor).synthesize('сегодня хорошая погода',Event())
    voice=soxr.resample(np.frombuffer(pcm.data,dtype='<i2').astype(np.float32),24000,16000)
    other_voice=soxr.resample(np.frombuffer(other.data,dtype='<i2').astype(np.float32),24000,16000)
    speech=np.pad(voice,(16000,16000))
    rng=np.random.default_rng(42);noise=rng.normal(0,350,len(speech)).astype(np.float32)
    overlapping=np.pad(other_voice,(16000,max(0,len(speech)-16000-len(other_voice))))[:len(speech)]
    stt=VoskRecognizer(settings.vosk_model)
    results=[]
    for label,source in [('synthetic_quiet',speech),('synthetic_noise',speech+noise),
                         ('noise_only',noise),('synthetic_overlap',speech+overlapping*.7)]:
        source=np.clip(source,-32768,32767).astype('<i2')
        processor=MicrophoneProcessor('fair');frames=[];started=perf_counter()
        try:
            for i in range(0,len(source)-511,512):
                frames.extend(processor.process(AudioFrame(source[i:i+512].tobytes(),(i+512)/16000)))
            milliseconds=(perf_counter()-started)*1000
            processed=np.frombuffer(b''.join(f.pcm for f in frames),dtype='<i2')
            raw_text=recognize(stt,source);clean_text=recognize(stt,processed)
            results.append({'case':label,'raw_text':raw_text,'processed_text':clean_text,
                'raw_wer':distance(reference,raw_text) if label!='noise_only' else None,
                'processed_wer':distance(reference,clean_text) if label!='noise_only' else None,
                'raw_activation':ActivationGate(settings).accept(raw_text,1,1) is not None,
                'processed_activation':ActivationGate(settings).accept(clean_text,1,1) is not None,
                'processing_ms':round(milliseconds,2),'audio_ms':round(len(source)/16,2),
                'buffered_audio_ms':round((len(source)-len(processed))/16,2)})
        finally:processor.close()
    output=ROOT/'.build-tmp'/'fair-audio-validation.json';output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps({'synthetic_only':True,'reference':reference,'cases':results},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(results,ensure_ascii=True,indent=2))


if __name__=='__main__':asyncio.run(run())
