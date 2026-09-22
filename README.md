# Codex Idle Compactor

A Mac utility that compacts eligible idle local Codex tasks using a **best-effort timing policy**. Timing does not guarantee a cache hit or zero uncached tokens. Install once, then receive **signed automatic updates** from this repository's releases. It uses your existing desktop app session and account.

**Compatibility is checked by capabilities, not exact app builds.** The utility reads the app's declared IPC methods and validates live task state. An app update that retains these contracts needs no utility update. If a contract changes, compaction pauses while the independent updater continues checking for a fix.

No third-party utility can guarantee that every future private interface will keep working. This utility does not claim that guarantee.

## Install

Download [the latest ZIP](https://github.com/intenex/codex-idle-compactor/releases/latest/download/idle-compactor.zip), unzip it, and open **Install.command**. This enables the 50-attempts-per-day policy described below and automatic updates. Alternatively, open **Install Observation.command** for a monitor that makes no compaction calls. Python 3.9+ is required; no third-party Python packages are needed.

The installer copies the signed release into `~/Library/Application Support/Codex Idle Compactor/` and installs a LaunchAgent that runs at login. The download folder can then be removed. The updater starts even if ChatGPT/Codex is closed or the currently installed app is incompatible.

Installing the new package also migrates an existing 0.1 installation while preserving its ledger. Copies of 0.1 distributed before automatic updating was added need this one-time upgrade.

## What it covers

- Local tasks owned by a compatible ChatGPT/Codex desktop app on the same Mac.
- Compatible desktop versions independent of their version/build number, including alternate app names and supported local database schema versions.
- Currently understood old and new task-state shapes, checked for idleness before dispatch.

It does **not** cover ordinary ChatGPT web/Classic conversations, cloud or remote tasks, standalone CLI processes, or subagents. Those sessions do not expose the same verified local compaction path. Run a copy on each Mac for local desktop tasks there. No separate app-server process is opened to mutate an existing session.

## Automatic updates

The independent update manager checks approximately every **30 minutes while the Mac is awake**, including when the app adapter is broken. It verifies an RSA/SHA-256 signature against the installed publisher key, checks the package digest, stages and self-checks the release, then switches versions at a worker boundary. The update manager itself is part of the updated release; a small recovery bootstrap remains installed.

Settings, approval, daily/monthly attempt accounting, and conversation history are preserved. The retired `cooldown_seconds` setting is ignored and removed on the next settings write. A broken startup rolls back to the previous installed release, and a later signed fix can recover it. Offline machines continue using their installed version and retry later. Downloads are bounded; failures do not create a tight retry loop.

Updates come from public GitHub releases. There is no hosted worker, paid update service, or GitHub Actions schedule. Updates do not upload your conversations or usage ledger. GitHub receives normal release-download requests.

## Compaction policy

| Setting | Default |
|---|---|
| Trigger | 25 minutes since the most recent recorded model usage |
| Latest dispatch | Strictly before 29 minutes since that usage; hard ceiling below 30 minutes |
| Context estimate | 1,024–1,000,000 tokens from the most recent request |
| Per-task cooldown | None; each newly completed turn can qualify again |
| Global attempts | 50/day (UTC); no monthly cap |
| Repeated cache warming | None |
| Ambiguous completion | No retry; block new attempts pending reconciliation |

A new model-usage event restarts the timer. Bookkeeping, delayed completion records, and duplicate usage reports do not. Busy tasks and pending user submissions are skipped. Missed windows after sleep are skipped. Already compacted or attempted turns are not compacted again without a new completed turn; there is no per-task daily waiting period. Pausing compaction leaves automatic software updates running. An already accepted compaction may finish after you pause.

The counter limits bound utility attempts, not model output tokens, provider retries, dollars, or subscription quota. Compaction can consume more tokens than it saves. Token savings have not been established. See [OpenAI's caching guide](https://developers.openai.com/api/docs/guides/prompt-caching) and [compaction guide](https://developers.openai.com/api/docs/guides/compaction).

### Timing and caching limits

The app records when model usage is reported, **not the actual provider cache-write/reuse timestamp**. That recorded response timestamp is the timer's proxy. A valid candidate has a completed turn and usage age of at least 25 minutes and less than 29 minutes. The utility checks again after live-state preflight, after saving its attempt reservation, and immediately before sending the desktop compaction request. It never intentionally dispatches a request aged 30 minutes or more by that recorded timestamp. A safely rejected late request does not block other tasks; ambiguous requests are still never retried.

This is explicitly a time-based policy, not a cache-status guarantee. Cache routing, changed prefixes, uncached suffixes, model-specific retention, and the gap between request processing and usage reporting can still cause uncached tokens. The app may also queue an accepted request. The utility cannot guarantee the actual server execution time or cache state. OpenAI's API guide describes 30 minutes as a **minimum** cache lifetime for GPT-5.6 and later, not a universal ChatGPT/Codex expiry timestamp. Compaction can change the cached prefix.

Manual `compact THREAD_ID` uses the same timing window. The `--allow-cold` override has been removed; there is no supported bypass for a missed window.

## Control and diagnostics

From any copy of this download folder:

```sh
python3 control.py status
python3 control.py update-status
python3 control.py update
python3 control.py pause
python3 control.py enable --accept-metered-compaction --max-per-day 50 --max-per-month none
python3 control.py metrics
python3 control.py reconcile
python3 control.py uninstall
```

`control.py` always targets the installed version. `update` requests an immediate check; normal checks happen automatically. `update-status` shows the installed version, update status, and whether the compactor is running or compatibility-blocked.

`status` and worker health identify the policy as `time-based-best-effort` and explicitly report `cache_hit_guaranteed: false`.

`metrics` reports observed usage where available; unknown usage stays null. `reconcile` recognizes a compaction that finished after a timeout without retrying it. The watcher also checks for late completion markers automatically; unresolved requests are never retried. The app's private operation has no atomic compare-and-compact condition, so a small race with new user input remains.

Uninstall removes the background service but preserves settings and the ledger. Do not delete the ledger merely to reset limits. Local reports contain task IDs and scalar metadata, never saved copies of prompts, responses, or credentials.

For publishing fixes to all installations, see [MAINTAINER.md](MAINTAINER.md). For actual evidence and limitations, see [VERIFICATION.md](VERIFICATION.md).
