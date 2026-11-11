# Silero background warmup

Goal: move model loading and two silent inferences ahead of the first voice reply.

Real-model validation showed a second slow execution after the first trial, so
two passes are needed to move both cold executions out of the reply path.

Use the existing dialog runtime and speech executor. Start Silero and provider
warmup concurrently when runtime warmup is enabled. Keep warmup cancellation
separate from turn cancellation. Discard all trial PCM; emit no dialogue events.
Cache successful warmup per loaded model, thread count and voice. Invalidate it
when weights are reloaded. Log background failures and let normal requests retry.

- [x] Add failing tests for silent warmup, reuse, retry, cancellation and overlap.
- [x] Implement cached Silero warmup and concurrent runtime startup preparation.
- [x] Run adapter/runtime regression tests and the full suite (439 tests passed).
- [x] Document behavior and verification limits, including real CPU measurements.
