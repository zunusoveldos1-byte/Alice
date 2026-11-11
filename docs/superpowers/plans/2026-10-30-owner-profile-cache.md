# Owner profile caching

Goal: avoid repeated reads, JSON parsing and normalization of an unchanged owner
profile during windowed speaker verification. Keep the same voice threshold and
activation rules. No settings or real owner profile changes.

Use a per-OwnerProfile lock and one cached normalized profile. Check file identity,
size and timestamps before reuse. Invalidate on failed reads, changed metadata,
enrollment and deletion; verify metadata again after reading. Return vector copies
so callers cannot mutate the cached identity.

- [x] Add failing tests for read reuse, replacement, deletion, races and isolation.
- [x] Implement cache and enrollment invalidation.
- [x] Run owner tests and full suite (466 passed); measure temporary-profile reads.
- [x] Document measured benefit and scope.
