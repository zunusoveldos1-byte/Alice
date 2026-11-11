# Hybrid STT Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans task-by-task.

**Goal:** Локально уточнять полные запросы через Whisper, сохранив быстрый Vosk.
**Architecture:** Отдельный bounded executor для PCM-фразы после endpoint;
частичные Vosk и прямые stop-команды идут прежним путём.
**Tech Stack:** Python, faster-whisper small CPU int8, PyQt6, Vosk.
**Spec:** ../specs/2026-10-31-hybrid-stt-design.md

## Constraints and review focus

Не блокировать capture; не доставлять устаревшие epoch/generation/revision;
не скачивать при запуске; не сохранять PCM; не обходить owner-проверку ярмарки.
Проверить преролл, паузы, отмену, переполнение, пустые результаты и ошибки.
Git в рабочем каталоге отсутствует: изменения остаются локальными.

## Task 1: Adapter and queue

- [x] Тесты в tests/test_whisper_stt.py: PCM→float, локальная загрузка,
  непустой исправленный текст, очередь/отмена/ошибка. Запустить RED.
- [x] Создать alice_voice/whisper_stt.py: WhisperRefiner и RefinementQueue.
- [x] Запустить GREEN.

## Task 2: Capture, settings, runtime checks

- [x] Интеграционные тесты CaptureWorker: исправленный финал, прямой stop,
  продолжение захвата во время inference, stale-result rejection. Запустить RED.
- [x] Добавить stt_backend/whisper_model в config и настройки UI;
  собрать bounded PCM и подключить очередь в workers.py.
- [x] Установить optional зависимость и подготовить локальную модель small.
- [x] Выполнить полный unittest, smoke, нативное сравнение.
- [x] Включить гибрид в текущей конфигурации после успешных проверок,
  сохранить отчёт и предложить живую проверку распознанного текста.

## Execution ledger

Adapter/queue and capture integration implemented. RED→GREEN for new
contracts, cancellation races and offline-model checks. Full suite 494 OK;
focused final 14 + 25 OK; smoke and pip check OK. SHA256 verified.

Ruling: preserve the interrupting utterance id and reject older partials
before cancellation; fresh review reproduced both races and rechecked fixes.
Ruling: no hotword correction or regex substitution: native small-model
comparison did not demonstrate better proper-name accuracy.

Native benchmark is recorded in docs/hybrid-stt-native-results.json.
Hybrid enabled; actual Alice launched for live speech check.
Live quality acceptance remains pending user feedback; task is not a claim
that all recognition errors have been fixed.
