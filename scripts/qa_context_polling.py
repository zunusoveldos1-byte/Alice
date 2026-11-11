"""Deterministic background-query count replay, without inspecting real windows."""
import json
from pathlib import Path
import sys
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from alice_voice.config import Settings
from alice_voice.context_tools import ToolRouter


class Desktop:
    def __init__(self):
        self.hwnd=10
        self.handle_checks=self.full_queries=0

    def foreground_window_id(self):
        self.handle_checks+=1
        return self.hwnd

    def active_window(self):
        self.full_queries+=1
        return {'hwnd':self.hwnd,'pid':-1,'title':'Example','exe':'example.exe','rect':[0,0,640,480]}


def replay(optimized,changes):
    desktop=Desktop()
    router=ToolRouter(Settings(),desktop=desktop)
    with patch('alice_voice.context_tools.monotonic') as clock:
        for tick in range(120):
            clock.return_value=tick*.5
            desktop.hwnd=10+tick//4 if changes else 10
            if optimized: router.poll_context()
            else: router.observe_context()
    return {'ticks':120,'simulated_seconds':60,'handle_probes':desktop.handle_checks,
            'full_metadata_queries':desktop.full_queries,'last_handle':router.last_context.get('hwnd')}


if __name__=='__main__':
    result={'real_windows_queried':False,'wall_clock_speed_measured':False,
            'stable':{'before':replay(False,False),'after':replay(True,False)},
            'switch_every_two_seconds':{'before':replay(False,True),'after':replay(True,True)}}
    path=Path('docs/context-polling-results.json')
    path.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))
