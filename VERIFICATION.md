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

Release deployment and automatic-upgrade evidence will be recorded after publication and local migration verification.
