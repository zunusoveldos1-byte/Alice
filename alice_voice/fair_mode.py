"""Explicit command activation and bounded physical hold, independent of STT partials."""
from collections import deque
import re
from threading import Lock
from .audio import FRAME_SECONDS

NOISY = 'Не расслышала. Повторите'


class LocalReply(str):
    """Trusted app-authored speech, never routed to a model or command tool."""


def enabled(settings):
    return settings.fair_mode or settings.noise_mode == 'fair'


def words(text):
    return re.findall(r'\w+',text.casefold().replace('ё','е'))


def protocol_command(text,wake_name='алиса'):
    tokens=words(text)
    if tokens[:1]==words(wake_name):tokens=tokens[1:]
    if tokens==['протокол','ярмарка']:return True
    if tokens in (['ярмарка','стоп'], ['стоп','ярмарка'],
                  ['выключи','ярмарку'], ['отключи','ярмарку'],
                  ['выключи','протокол','ярмарка'], ['отключи','протокол','ярмарка']):return False
    return None


class ActivationGate:
    def __init__(self,settings):
        self.settings=settings
        self.deadline=0.
        self.opened=0.

    def reset(self):
        self.deadline=self.opened=0.

    def accept(self,text,start,now):
        tokens=words(text)
        name=self.settings.wake_name.casefold().replace('ё','е')
        if self.settings.wake_enabled and tokens and tokens[0]==name:
            self.opened=now
            self.deadline=now+self.settings.wake_window_s
            if len(tokens)==1:return ''
            self.reset()
            return re.sub(r'^\W*\w+\b[\s,.:;!?-]*','',text,count=1,flags=re.I)
        if tokens and self.opened <= start <= self.deadline and self.deadline:
            self.reset()
            return text
        if now>self.deadline:self.reset()
        return None


class HoldToTalk:
    """UI writes only intent under a lock; capture owns buffers and PCM."""
    def __init__(self,preroll_ms=320,max_seconds=30):
        self.lock=Lock()
        self.preroll=deque(maxlen=max(1,(preroll_ms+31)//32))
        self.limit=max_seconds
        self.request=None
        self.active=None
        self.used=None

    def press(self,token,when,not_before=0):
        with self.lock:self.request={'token':token,'down':when,'up':None,'floor':not_before}

    def release(self,when):
        with self.lock:
            if self.request:self.request['up']=when

    def reset(self):
        self.preroll.clear()
        if self.active is not None:self.used=self.active['token']
        self.active=None

    @property
    def pending(self):
        with self.lock:return self.request is not None and self.request['token']!=self.used

    def feed(self,frame,probability):
        with self.lock:req=dict(self.request) if self.request else None
        result=None
        if req and req['token']!=self.used and self.active is None and frame.captured_at-FRAME_SECONDS>=req['floor'] and frame.captured_at>=req['down']:
            self.active=dict(req,voiced=0,start=frame.captured_at-FRAME_SECONDS)
            frames=[f for f in self.preroll if f.captured_at-FRAME_SECONDS>=req['floor']]+[frame]
            result={'kind':'start','frames':frames,'token':req['token']}
        elif self.active:
            result={'kind':'audio','frames':[frame],'token':self.active['token']}
        if self.active:
            if probability>=.5:self.active['voiced']+=1
            up=req['up'] if req and req['token']==self.active['token'] else self.active['start']
            if (up is not None and frame.captured_at>=up+.096) or frame.captured_at-self.active['start']>=self.limit:
                result.update(kind='finish',start=self.active['start'],voiced=self.active['voiced'])
                self.used=self.active['token'];self.active=None
        self.preroll.append(frame)
        return result


def hotkey_down(sequence):
    """Read physical state only; never inject or consume keyboard input."""
    import ctypes
    parts=sequence.split('+')
    keys=[{'Ctrl':0x11,'Alt':0x12,'Shift':0x10}[p] for p in parts[:-1]]
    keys.append(0x70+int(parts[-1][1:])-1)
    return all(ctypes.windll.user32.GetAsyncKeyState(key)&0x8000 for key in keys)
