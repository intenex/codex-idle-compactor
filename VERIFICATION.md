# Verification

## Implemented and checked

- Original compaction safeguards: idle timing, busy-state rejection, durable attempt reservations, daily/monthly limits, cooldown, pause, no retry on unknown outcomes, and completion-marker verification.
- Capability discovery from ASAR metadata without executing bundled JavaScript. Build numbers are diagnostic, not an allowlist.
- Synthetic future build and snapshot-version fixtures, older completed-turn state, and newer state-database filenames.
- Live read-only IPC handshake and task-state checks against the installed ChatGPT/Codex desktop app.
- Signed-update verification, modified manifest rejection, digest mismatch rejection, rollback/reused-sequence prevention, unsafe ZIP entries, failed release self-checks, offline throttling, local rollback, later-fix recovery, and preservation of settings/ledger.

## Evidence limits

Fixtures spanning multiple protocol/build shapes are not real execution tests of every historical or future app release. Current live coverage is the installed Mac desktop app. Ordinary ChatGPT web/Classic conversations, remote/cloud tasks, and standalone CLI processes are outside the implemented compaction path.

No claim is made that compaction always saves tokens or that private app interfaces cannot break. The updater remains separate from those interfaces so a signed fix can be delivered after a break. An uncertain already-dispatched model operation may still require manual reconciliation before compaction resumes.

## Live evidence on 2026-09-22

- 53 automated tests pass, including the 50/day limit, no monthly ceiling, persistence across restarts, pause, signed updates, failed signatures, rollback, installation timing, liveness before a slow first scan, and late-completion reconciliation.
- Published signed releases to the public GitHub feed and observed the installed background service upgrade itself from 0.2.0 to 0.2.1. The previously configured limits stayed unchanged until the explicit owner request was applied.
- Active owner configuration: enabled, 50 attempts/day (UTC), no monthly cap, context range 1,024–1,000,000 tokens. Account billing and plan were not changed.
- One explicitly requested manual live compaction of an already idle, app-owned task completed. Completion was verified from the persisted compaction marker, not merely the IPC acknowledgement.
- That task had a 163,202-token pre-compaction request estimate. The compaction request itself recorded 163,459 input tokens, including 15,104 cached and 148,355 uncached, plus 2,222 output tokens. The live app reported 8,754 context tokens afterward. This was a cold manual test beyond the normal automatic window. It does not demonstrate net token savings; no subsequent user turn was created just to measure savings.
- The desktop app used for the real test was ChatGPT/Codex 26.908.40834, build 8881. Future/older protocol shapes have fixture coverage, not real execution coverage for every released build.

Final deployment status is maintained in the repository documentation; release archives retain the verification record available when they were signed.
