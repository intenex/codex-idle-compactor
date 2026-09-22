# Codex Idle Compactor

A Mac utility that can compact eligible idle local Codex tasks. Install once, then receive **signed automatic updates** from this repository's releases. It uses your existing desktop app session and account.

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

Settings, approval, daily/monthly attempt accounting, and conversation history are preserved. A broken startup rolls back to the previous installed release, and a later signed fix can recover it. Offline machines continue using their installed version and retry later. Downloads are bounded; failures do not create a tight retry loop.

Updates come from public GitHub releases. There is no hosted worker, paid update service, or GitHub Actions schedule. Updates do not upload your conversations or usage ledger. GitHub receives normal release-download requests.

## Compaction policy

| Setting | Default |
|---|---|
| Idle threshold | 25 minutes since recorded activity |
| Latest dispatch | Before 29 minutes of inactivity |
| Context estimate | 1,024–1,000,000 tokens from the most recent request |
| Per-task cooldown | 24 hours |
| Global attempts | 50/day (UTC); no monthly cap |
| Repeated cache warming | None |
| Ambiguous completion | No retry; block new attempts pending reconciliation |

Assistant/tool activity resets the quiet period. Busy tasks are skipped. Missed windows after sleep are skipped. Pausing compaction leaves automatic software updates running. An already accepted compaction may finish after you pause.

The counter limits bound utility attempts, not model output tokens, provider retries, dollars, or subscription quota. Compaction can consume more tokens than it saves. Token savings have not been established. See [OpenAI's caching guide](https://developers.openai.com/api/docs/guides/prompt-caching) and [compaction guide](https://developers.openai.com/api/docs/guides/compaction).

A maintenance-only `compact THREAD_ID --allow-cold` command can explicitly compact one task beyond the normal window while the watcher is stopped. It still requires 25 minutes of idleness, live desktop ownership, and remaining attempt allowance. Cold compaction may consume uncached tokens.

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

`metrics` reports observed usage where available; unknown usage stays null. `reconcile` recognizes a compaction that finished after a timeout without retrying it. The watcher also checks for late completion markers automatically; unresolved requests are never retried. The app's private operation has no atomic compare-and-compact condition, so a small race with new user input remains.

Uninstall removes the background service but preserves settings and the ledger. Do not delete the ledger merely to reset limits. Local reports contain task IDs and scalar metadata, never saved copies of prompts, responses, or credentials.

For publishing fixes to all installations, see [MAINTAINER.md](MAINTAINER.md). For actual evidence and limitations, see [VERIFICATION.md](VERIFICATION.md).
