# Capture model reuse

Goal: reuse Silero VAD ONNX sessions and Vosk weights across microphone restarts.
Keep per-stream recurrent state, context, recognizers and buffers independent.

Use one thread-safe, latest-entry cache per adapter. Key entries by resolved
path and file metadata, including nested files for Vosk. Publish only successful
loads. Retain at most one model in each cache; live streams may still hold older
models until they stop. No changes to microphone selection or activation rules.

- [x] Test reuse, state isolation, concurrency, retry and changed model files.
- [x] Implement native-model caching without caching stream state.
- [x] Verify adapter tests and full suite (447 tests passed).
- [x] Measure local model startup and document memory/speed tradeoff.
