# Whisper small / faster-whisper

Локальная модель [Systran/faster-whisper-small](https://huggingface.co/Systran/faster-whisper-small)
— конверсия OpenAI Whisper small в формат CTranslate2; model card указывает
лицензию MIT. Веса не изменялись; int8 выбирается при загрузке в RAM.
model.bin: 483 546 902 байта, SHA256
`3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671`.
Модель скачана отдельно и исключена из исходного кода через `models/`.

Опциональная библиотека faster-whisper 1.2.1: Copyright (c) 2023 SYSTRAN,
[MIT](https://github.com/SYSTRAN/faster-whisper/blob/master/LICENSE).
Её зависимости устанавливаются со своими уведомлениями в Python-пакетах.

# Silero TTS v4_ru

Автор: Silero Team / snakers4, проект [silero-models](https://github.com/snakers4/silero-models).
Используется неизменённый пакет `v4_ru` с официального адреса
https://models.silero.ai/models/tts/ru/v4_ru.pt, 40 107 184 байта.
SHA-256: `896ab96347d5bd781ab97959d4fd6885620e5aab52405d3445626eb7c1414b00`.
Это фиксирует конкретные проверенные байты; обновлённые веса с другим хэшем
автоматически не принимаются. Изменений весов не производилось.

По разделу Licence официального README обычные модели наследуют основную
лицензию репозитория; исключение MIT относится к CIS base, не к v4_ru.
README сокращённо называет её CC-NC-BY, но **фактический LICENSE начинается
с Attribution-NonCommercial-ShareAlike 4.0 International**. В Alice применены
условия этого полного текста — CC BY-NC-SA 4.0.

При использовании/распространении весов соблюдайте некоммерческое ограничение,
указывайте авторство и источник, ссылку на лицензию, сохраняйте уведомления
и указывайте изменения, если они сделаны. При распространении адаптированных
весов применяется ShareAlike. Коммерческое использование требует отдельного
разрешения правообладателя. Отдельная загрузка в кэш не отменяет этих условий.
Это уведомление касается весов и их адаптаций, не объявляет лицензию всего
кода Alice. Веса не добавлены в дистрибутив исходного кода; `models/` исключён.

- [Официальный LICENSE](https://github.com/snakers4/silero-models/blob/master/LICENSE).
- [Локальная копия полного текста](licenses/silero-models-CC-BY-NC-SA-4.0.txt).
- [Официальные версии и sample rates](https://github.com/snakers4/silero-models/blob/master/models.yml).
- [V4, голоса и русский язык](https://github.com/snakers4/silero-models#v4).

PyTorch CPU 2.10.0 используется как отдельная зависимость, распространяемая
со своими уведомлениями в пакете PyTorch; GPU/CUDA не требуются.
