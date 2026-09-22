# Verification

## Implemented and checked

- Compaction safeguards: idle timing, busy-state rejection, durable attempt reservations, daily/monthly limits, pause, no retry on unknown outcomes, and completion-marker verification. The extra per-task cooldown was removed in 0.2.5.
- Capability discovery from ASAR metadata without executing bundled JavaScript. Build numbers are diagnostic, not an allowlist.
- Synthetic future build and snapshot-version fixtures, older completed-turn state, and newer state-database filenames.
- Live read-only IPC handshake and task-state checks against the installed ChatGPT/Codex desktop app.
- Signed-update verification, modified manifest rejection, digest mismatch rejection, rollback/reused-sequence prevention, unsafe ZIP entries, failed release self-checks, offline throttling, local rollback, later-fix recovery, and preservation of settings/ledger.

## Evidence limits

Fixtures spanning multiple protocol/build shapes are not real execution tests of every historical or future app release. Current live coverage is the installed Mac desktop app. Ordinary ChatGPT web/Classic conversations, remote/cloud tasks, and standalone CLI processes are outside the implemented compaction path.

No claim is made that compaction always saves tokens or that private app interfaces cannot break. The updater remains separate from those interfaces so a signed fix can be delivered after a break. An uncertain already-dispatched model operation may still require manual reconciliation before compaction resumes.

## Current policy: 0.2.5

- 62 automated tests pass. Added checks cover repeated completed turns in the same task without a cooldown, migration of old cooldown settings, rejection of the removed cold override, and the retained 50/day cap with no monthly cap. Missing, non-finite, and future usage timestamps cannot dispatch.
- Timing is measured from the latest recorded model usage, not later bookkeeping. Duplicated response IDs and unchanged legacy usage reports do not refresh that clock. The app does not expose the actual cache-write/reuse timestamp, so this remains a proxy.
- Boundary checks cover ages 24:59, 25:00, 28:59, 29:00, 30:00 and later, plus late preflight and reservation. A real Unix-socket fixture allows one timely request and verifies no late requests are sent. Tests never call a model.
- A safely rejected request after a slow reservation counts against the daily cap but does not block unrelated tasks; uncertain dispatch still blocks retries. Live desktop idleness, pending input, scope, and completion checks remain.
- Live read-only IPC verification passed on ChatGPT/Codex 26.908.40834, build 8881. Usage records provide response reporting timestamps and token counters; no actual cache-expiration timestamp is exposed on the inspected path.
- The desktop interface has no verified server-enforced cache-only operation. The owner explicitly chose timing alone; the utility reports that cache hits are not guaranteed. No claim of zero uncached tokens or net savings is made.

## Historical live evidence on 2026-09-22 (0.2.4 and earlier)

- 53 automated tests pass, including the 50/day limit, no monthly ceiling, persistence across restarts, pause, signed updates, failed signatures, rollback, installation timing, liveness before a slow first scan, and late-completion reconciliation.
- Published signed releases to the public GitHub feed and observed the installed background service upgrade itself from 0.2.0 to 0.2.1. The previously configured limits stayed unchanged until the explicit owner request was applied.
- Then-active owner configuration: enabled, 50 attempts/day (UTC), no monthly cap, context range 1,024–1,000,000 tokens. A later clarification selected timing-based operation; see the current policy above. Account billing and plan were not changed.
- One explicitly requested manual live compaction of an already idle, app-owned task completed. Completion was verified from the persisted compaction marker, not merely the IPC acknowledgement.
- That task had a 163,202-token pre-compaction request estimate. The compaction request itself recorded 163,459 input tokens, including 15,104 cached and 148,355 uncached, plus 2,222 output tokens. The live app reported 8,754 context tokens afterward. This was a cold manual test beyond the normal automatic window. It does not demonstrate net token savings; no subsequent user turn was created just to measure savings.
- The desktop app used for the real test was ChatGPT/Codex 26.908.40834, build 8881. Future/older protocol shapes have fixture coverage, not real execution coverage for every released build.

The service previously upgraded automatically from 0.2.2 to 0.2.4, reported healthy worker and supervisor state, and promoted 0.2.4 to its last known good release. Settings and the ledger containing the completed real attempt retained identical SHA-256 digests through that upgrade. The LaunchAgent remains installed; current compaction behavior is governed by the timing policy above.

Final deployment status is maintained in the repository documentation; release archives retain the verification record available when they were signed.
