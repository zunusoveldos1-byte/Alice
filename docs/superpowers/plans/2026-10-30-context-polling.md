# Background window observation

Goal: reduce full desktop metadata queries without losing focus-change detection.

Check only the foreground window handle every 0.5 seconds. Refresh full metadata
on a changed handle or after 5 seconds (titles can change without a new handle).
Keep command-time observation unconditional and never return background data as
current focus evidence. Serialize background and command observation with RLock.

- [x] Add failing tests for stable focus, changes, refresh, failures and commands.
- [x] Route the background watcher through cheap handle checks.
- [x] Verify focused tests and full suite (457 passed); measure call counts.
- [x] Document behavior, native Windows smoke check and limits.
