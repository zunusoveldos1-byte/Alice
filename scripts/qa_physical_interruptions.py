"""Opt-in human-operated physical microphone/output checks, no cloud claims."""
import argparse
from dataclasses import replace
import json
import logging
from pathlib import Path
import sys
from threading import Event
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt6.QtCore import QTimer
from PyQt6.QtMultimedia import QMediaDevices, QAudio
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel, QPushButton, QCheckBox, QComboBox
from alice_voice.config import Settings
from alice_voice.controller import VoiceSessionController
from alice_voice.speech import sapi_synthesize
from alice_voice.workers import CaptureWorker

logging.basicConfig(filename='.build-tmp/physical-interruptions-2026-10-31.log', encoding='utf-8', level=logging.INFO)
parser = argparse.ArgumentParser()
parser.add_argument('--mode', choices=['headphones', 'speakers'], required=True)
parser.add_argument('--case', type=int, choices=(0, 1, 2), default=0)
parser.add_argument('--output', type=Path, default=Path('docs/physical-interruptions-2026-10-31.json'))
args = parser.parse_args()
app = QApplication([])
devices = list(QMediaDevices.audioOutputs())
selected = QMediaDevices.defaultAudioOutput().description()
original = Settings.load('settings.json')
settings = replace(original, synthesizer='sapi', fair_mode=False, owner_only=False, headphones=args.mode == 'headphones',
                   output_device=selected, volume=.5, silence_ms=650)
c = VoiceSessionController(settings)
c.timer.stop()
window = QWidget()
window.setWindowTitle('Alice — проверка прерываний 31 октября (локальный SAPI)')
window.resize(750, 370)
layout = QVBoxLayout(window)
label = QLabel('Режим: ' + args.mode + '\nВыход: ' + selected + '\nМикрофон: текущий вход Alice.\n'
               'LLM здесь тестовый; SAPI, микрофон, VAD/STT и аудиовыход настоящие.\n'
               'Выполните три сценария по очереди. Аудиозаписи на диск не сохраняются.')
label.setWordWrap(True); layout.addWidget(label)
case = QComboBox(); case.addItems(['Молчать: проверить собственную озвучку', 'Сказать: Стоп', 'Сказать: Найди погоду в Бишкеке'])
case.setCurrentIndex(args.case)
layout.addWidget(case)
output_choice = QComboBox()
output_choice.addItems([d.description() for d in devices])
output_choice.setCurrentText(selected)
layout.addWidget(QLabel('Выход для проверки (выберите свои наушники):'))
layout.addWidget(output_choice)
physical = QCheckBox('Использую физические ' + ('наушники' if args.mode == 'headphones' else 'динамики') + ', слышу выбранный выход')
layout.addWidget(physical)
start = QPushButton('Начать сценарий'); layout.addWidget(start)
status = QLabel('Готово к началу. Не говорите до появления озвучки.'); status.setWordWrap(True); layout.addWidget(status)
heard = QCheckBox('Я слышал(а) озвучку'); layout.addWidget(heard)
stopped = QCheckBox('При моей фразе звук остановился; либо в сценарии молчания звучал до конца')
layout.addWidget(stopped)
save = QPushButton('Сохранить наблюдение и следующий сценарий'); save.setEnabled(False); layout.addWidget(save)
finish = QPushButton('Закончить и закрыть'); layout.addWidget(finish)
rows = []; active = None; closing = False
answer = ('Это проверка собственной озвучки. Стоп. Замолчи. Хватит. '
          'Эти слова сейчас произносит ассистент. Я продолжаю рассказывать о погоде. '
          'Сегодня солнечно, завтра возможен небольшой дождь. '
          'В Бишкеке утром прохладно, а днём станет теплее. ')
carrier = sapi_synthesize(answer, 'Microsoft Irina Desktop - Russian', Event(), 20)
plain_answer = ('Я продолжаю рассказывать о погоде. Сегодня солнечно и тепло. '
                'Завтра возможен небольшой дождь. Утром прохладно, днём станет теплее. '
                'В городе ожидается слабый ветер. Сейчас можно спокойно планировать прогулку. ')
plain_carrier = sapi_synthesize(plain_answer, 'Microsoft Irina Desktop - Russian', Event(), 20)

class Runtime:
    def cancel(self):
        if active is not None: active['cancellations'].append(perf_counter())
    def submit(self, token, text):
        if active is not None: active['submitted'].append(text)
        return True
    def release_pcm(self, token, sequence): pass
c.runtime = Runtime()

def evidence(epoch, value):
    if active is not None:
        active['evidence'].append({key: value.get(key) for key in ('text', 'acoustic', 'stable', 'start')})
def phrase(epoch, value):
    if active is not None: active['recognized'].append(value['text'])
def error(message):
    if active is not None: active['errors'].append(message)
    status.setText(message)
c.signals.voice_evidence.connect(evidence)
c.signals.phrase.connect(phrase)
c.notification.connect(error)

def native_state(state):
    if active is not None and state == QAudio.State.StoppedState:
        active['native_stopped'].append(perf_counter())

def launch_audio():
    if active is None: return
    if c.mic_state != 'listening':
        if perf_counter() - active['opened'] < 15:
            QTimer.singleShot(100, launch_audio); return
        active['errors'].append('Microphone readiness timeout'); end_trial(); return
    token = c.guard.begin('physical-' + str(len(rows)))
    c.capture.response_active.set()
    c.answer = answer if active['case'] == 0 else plain_answer
    active['assistant_contains_control_words'] = active['case'] == 0
    c.speech_end = perf_counter()
    active['audio_launch'] = perf_counter()
    c._pcm(token, carrier if active['case'] == 0 else plain_carrier, perf_counter(), 0)
    if c.playback.sink is not None:
        c.playback.sink.stateChanged.connect(native_state)
    status.setText('ОЗВУЧКА: ' + case.currentText() + '\nПосле фразы подождите распознавания.')
    QTimer.singleShot(250, monitor)

def monitor():
    if active is None or active.get('ended'): return
    elapsed = perf_counter() - active['audio_launch']
    if active['native_stopped']:
        # A new sentence and its Whisper pass may outlast the output stop.
        # Keep capture alive until the final request arrives (bounded by 35 s).
        done = bool(active['submitted']) if active['case'] == 2 else perf_counter() - active['native_stopped'][0] >= 2.0
        if done or elapsed > 35: end_trial(); return
    elif c.playback.buffer is None or elapsed > 35:
        end_trial(); return
    QTimer.singleShot(100, monitor)

def end_trial():
    if active is None: return
    active['ended'] = True
    active['audio_damage_counts'] = c.capture.inbox.damage_counts if c.capture else {}
    active['latency'] = c.metrics.summary()
    active['pcm_queue_empty'] = not c.pcm_queue and c.current_chunk is None and c.pcm_bytes == 0
    active['buffer_released'] = c.playback.buffer is None
    status.setText('Распознано: ' + repr(active['recognized']) + '\nСледующий запрос: ' + repr(active['submitted']) +
                   '\nПрерываний: ' + str(len(active['cancellations'])) + '. Отметьте наблюдение и сохраните.')
    if c.capture is not None: c.capture.stop()
    save.setEnabled(True)

def begin():
    global active, selected, settings
    if not physical.isChecked(): status.setText('Сначала подтвердите физическое устройство.'); return
    if c.capture is not None and c.capture.is_alive(): status.setText('Подождите завершения предыдущего захвата.'); return
    c.capture = None
    c.cancel_response()
    from alice_voice.metrics import LatencyMetrics
    c.metrics = LatencyMetrics()
    selected = output_choice.currentText()
    settings = replace(settings, output_device=selected)
    c.settings = settings
    active = {'case': case.currentIndex(), 'instruction': case.currentText(), 'mode': args.mode,
              'physical_device_confirmed': physical.isChecked(), 'output': selected,
              'opened': perf_counter(), 'cancellations': [], 'native_stopped': [],
              'recognized': [], 'submitted': [], 'evidence': [], 'errors': []}
    c.epoch += 1
    c.capture = CaptureWorker(settings, c.signals, c.metrics, c.epoch)
    c.capture.playback_reference = c.playback_reference
    c.capture.start()
    heard.setChecked(False); stopped.setChecked(False)
    start.setEnabled(False); save.setEnabled(False); case.setEnabled(False); output_choice.setEnabled(False)
    status.setText('Загрузка микрофона/VAD/STT; дождитесь озвучки.')
    QTimer.singleShot(100, launch_audio)

def store():
    global active
    if active is None: return
    active['human_heard_output'] = heard.isChecked()
    active['human_behavior_confirmed'] = stopped.isChecked()
    rows.append(active); active = None
    c.cancel_response()
    result = {'mode': args.mode, 'real_microphone': True, 'real_sapi': True,
              'real_llm': False, 'real_elevenlabs': False, 'audio_saved': False,
              'acoustic_timing_measured': False, 'silence_ms': 650, 'rows': rows}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'saved_case': rows[-1]['case'], 'mode': args.mode,
                      'recognized': rows[-1]['recognized'], 'submitted': rows[-1]['submitted'],
                      'interruptions': len(rows[-1]['cancellations']),
                      'human_confirmed': rows[-1]['human_behavior_confirmed']}, ensure_ascii=False), flush=True)
    output_choice.setEnabled(True); case.setEnabled(True); case.setCurrentIndex(min(2, case.currentIndex() + 1))
    start.setEnabled(True); save.setEnabled(False)
    status.setText('Сохранено. Выполнено сценариев: ' + str(len(rows)) + '. Выберите следующий или закройте окно.')

def close_check():
    global active, closing
    if closing: return
    closing = True
    active = None
    if c.capture is not None: c.capture.stop(); c.capture.join(3)
    c.capture = None; c.runtime = None
    c.shutdown()
    app.quit()
start.clicked.connect(begin); save.clicked.connect(store); finish.clicked.connect(close_check)
app.aboutToQuit.connect(close_check)
window.show()
app.exec()
