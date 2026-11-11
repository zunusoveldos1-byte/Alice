# Alice Voice Implementation Plan

Goal: устойчивый голосовой диалог в RAM на Windows.
Architecture: схема и владение ресурсами в [design.md](design.md).
Tech Stack: Python 3.11+, PyQt6, sounddevice, ONNX Runtime, Vosk, httpx,
Edge-TTS, miniaudio, pywin32. Выполнение непосредственно в текущей сессии.

## Global constraints
PCM16 little-endian, 16000 Hz, mono, 512 samples; queue 32.
Никаких временных аудиофайлов, автоматических загрузок и скрытых fallback.

## Review focus
Переполнение посреди слова: отбросить фразу и дождаться тишины.
Поздний результат после отключения/отмены: не отображать и не воспроизводить.
COM cancellation: дождаться выхода executor до завершения приложения.
Обрыв HTTP или неверный ответ: безопасная ошибка без секрета.
Пауза на выходном устройстве: не освобождать QBuffer и позицию.

## Tasks
1. tests/test_core.py → audio.py, recognition.py, metrics.py, config.py.
   Frame(pcm, captured_at), bounded inbox, phrase segmenter, Vosk feed/final,
   точные стоп-команды. Сначала unittest RED, затем GREEN.
2. tests/test_services.py → providers.py, speech.py, secrets.py.
   async generate(messages, first_token), synthesize(text, cancel).
   Моки HTTP, COM, Edge. Ограничения буферов, токенов, timeout, cancellation.
3. tests/test_controller.py → workers.py, controller.py, playback.py.
   Открытый capture worker, generation guard, queued signals, cooperative
   shutdown. Проверить отмену и независимость состояний.
4. ui.py, __main__.py, README.md, --smoke и scripts/hardware_check.py.
   Запуск окна без микрофона, безопасное закрытие; отдельная проверка
   устройств только по явной команде. Итоговая полная unittest-проверка.

## Execution ledger
Рабочая папка пустая и не является Git-репозиторием; новая структура создаётся
непосредственно здесь. Пользователь уже задал порядок «предложи, затем
реализуй»; дополнительные согласования архитектуры не добавляются.

Task 1: complete — callback/inbox/ONNX/Vosk/segmentation/metrics.
Task 2: complete — async LLM, Credential Manager, memory SAPI/Edge.
Task 3: complete — generations, queued signals, async workers, playback.
Task 4: complete — PyQt UI, launcher, README, diagnostics, dependency lock.
Final review: independent reviewer; fixed overflow during finalization,
queued recognition after stop, rounded preroll echo boundary, incremental
bounded PCM decode. Covered by RED→GREEN and final suite 33/33.
Validation: Windows GUI close, actual PCM microphone, native Russian SAPI
and QAudioSink pause/resume/shutdown. Full dialog unavailable without models
and LLM; no end-to-end latency claim. See validation.md.
