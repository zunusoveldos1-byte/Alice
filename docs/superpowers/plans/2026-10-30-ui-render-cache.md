# UI rendering optimization

Goal: animate the existing UI without rebuilding unchanged text each frame.
Keep appearance, audio-level feedback, clock updates, resizing and mini window.

- [x] Add failing Qt tests for idle animation, cached text fit and live levels.
- [x] Measure the current frame loop without microphone or providers.
- [x] Separate visual-frame updates from dirty content rendering; cache replica fit.
- [x] Run focused UI tests and the full suite (450 tests passed); repeat measurement.
- [x] Document results, visual inspection and measurement limits.
