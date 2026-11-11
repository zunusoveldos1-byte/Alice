# Dialogue continuity implementation plan

> Execute inline with superpowers:executing-plans, test first.

**Goal:** Preserve conversational context and reduce avoidable time to first speech.
**Architecture:** Add shared RAM context to the existing runtime/router; retain transport,
recognition and playback implementations. Extractive compaction requires no LLM call.
**Tech Stack:** Python, asyncio, PyQt6, existing HTTP/TTS adapters.
**Spec:** ../specs/2026-10-23-dialogue-continuity-design.md

## Constraints and review focus

No interface changes, dependencies, new autonomous workflows or AEC claims.
Check corrections, stale windows, topic changes, cancelled tools and cold TTS overlap.

## Tasks

- [x] Freeze baseline in .build-tmp/dialogue-before and record reproducible paired timings.
- [x] Add tests then implement conversation.py: exact recent messages, whole-sentence
  extractive summary at eviction, task state and cached preferences.
- [x] Connect shared context in controller/workers/router; preserve it across reconfigure,
  use explicit action statuses and freshly observed window, resolve safe continuations,
  clarify missing/ambiguous targets. Verify ordinal model selection through SettingsStore.
- [x] Add tests then harden streaming filter/chunker, overlap Silero preparation with
  generation, instrument text/gap stages, retain cancellation and PCM ownership checks.
- [x] Run focused regressions and project suite with bounded per-module timeout, repeat
  the paired benchmark and attempt actual local services. Document measured limits.

Each implementation task uses a failing behavioural test followed by the minimal change
and a passing focused run. No Git repository exists in this workspace, so no commits.

## Added owner-memory requirement and rulings

- User expanded the active task to repair persistent owner identity and requested a
  facts UI; this authorizes that specific interface addition despite the earlier
  instruction to preserve unrelated interface components.
- Back up before migrating. Confirm owner name only from the user's explicit current
  requirement. Preserve unknown legacy rows for review; never claim provenance that
  the old schema did not store. Facts UI and voice writes use revision checks.
- Summary compaction is extractive and only at eviction. No new automatic persistent
  conversation archive or model-generated profile extraction was introduced.
- Native measurements exposed a shared speech-executor queue; independent context
  reading was added with a reproducing test. Acoustic/end-of-speech timing was not
  substituted with text-replay numbers; limits and warm regressions are documented.
- Fresh code review found profession-as-name parsing, stale ordinal choice hijacking
  and inherited test data-directory risk. All were fixed and regression-tested.
