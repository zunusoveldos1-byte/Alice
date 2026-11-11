"""Explicit local diagnostic: real model/PCM and offscreen UI, no playback or LLM."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import logging
import os
from pathlib import Path
import sys
from threading import Event
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication
from alice_voice.config import Settings
from alice_voice.controller import VoiceSessionController
from alice_voice.silero_tts import SileroSynthesizer, _CACHE
from alice_voice.ui.dialogs import SettingsDialog
from alice_voice.ui.main_window import MainWindow


async def check(app):
    settings = replace(Settings.load(ROOT / 'settings.json'), synthesizer='silero')
    controller = VoiceSessionController(settings)
    controller.timer.stop()
    window = MainWindow(controller, ROOT / 'settings.json')
    statuses = []
    first_cancel = Event()
    original_loader = _CACHE.loader
    def load(path, threads):
        first_cancel.set()  # Stop the first utterance while real initialization continues.
        return original_loader(path, threads)
    _CACHE.loader = load
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = SileroSynthesizer(settings, executor)
            second = SileroSynthesizer(settings, executor)
            assert first.cache is second.cache is _CACHE
            results = await asyncio.gather(first.prepare(first_cancel, statuses.append),
                                           second.prepare(Event(), statuses.append), return_exceptions=True)
            assert isinstance(results[0], asyncio.CancelledError)
            assert results[1] is None
            for page in ('dialog', 'work', 'voice'):
                window.select_page(page)
                app.processEvents()
                pcm = await SileroSynthesizer(settings, executor).synthesize('Проверка повторного ответа.', Event())
                assert pcm.data and pcm.sample_rate == 24000
            with patch('alice_voice.ui.dialogs.MicrophoneDiscovery.start'):
                dialog = SettingsDialog(settings)
                dialog.show(); app.processEvents(); dialog.close()
            window._open_mini(); app.processEvents()
            assert window.mini.controller is controller
            window._restore_main(); app.processEvents()
            stop = Event()
            model = _CACHE.models[settings.silero_threads]
            render = model.apply_tts
            def stopped_render(**kwargs):
                result = render(**kwargs)
                stop.set()
                return result
            model.apply_tts = stopped_render
            try:
                try:
                    await second.synthesize('Отменённая речь.', stop)
                    raise AssertionError('Cancelled PCM was returned')
                except asyncio.CancelledError:
                    pass
            finally:
                model.apply_tts = render
        # A recreated dialog runtime has a new executor and adapter, the same model service.
        with ThreadPoolExecutor(max_workers=1) as executor:
            await SileroSynthesizer(settings, executor).prepare(Event(), statuses.append)
            pcm = await SileroSynthesizer(settings, executor).synthesize('Ответ после остановки.', Event())
            assert pcm.data
        assert _CACHE.initializations == 1
        assert statuses.count('silero_loading') == 1
        logging.info('stage=silero_lifecycle_verified initializations=1 completed_pcm=4 cancelled_pcm=1 pages=3 mini=1 settings=1 executors=2')
    finally:
        _CACHE.loader = original_loader
        window.allow_close = True
        window.close(); controller.shutdown(); app.processEvents()


if __name__ == '__main__':
    os.chdir(ROOT)
    output = ROOT / '.build-tmp' / 'silero-lifecycle.log'
    output.parent.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(message)s',
                        handlers=[logging.StreamHandler(), logging.FileHandler(output, mode='w', encoding='utf-8')])
    logging.getLogger('alice_voice.silero_tts').setLevel(logging.DEBUG)
    app = QApplication([])
    asyncio.run(check(app))
