# Publish a fix to everyone

The update source is the latest non-prerelease release in `intenex/codex-idle-compactor`. Clients check its `update.json` asset every 30 minutes. A fix to the app adapter, compactor, updater, or supervisor can be delivered without another download by the user.

## Release procedure

1. Edit the code and add regression coverage for the incompatible app behavior. Treat installed JavaScript as data when discovering capabilities; never execute it in the updater. Do not remove idle checks or spend limits to force compatibility.
   Preserve the user-approved time-based policy: use the latest model-usage timestamp, trigger at 20 minutes, and stop dispatch before 25 minutes. Never describe this as a guaranteed cache hit. There is no per-task cooldown or cold-compaction bypass to restore.
2. Increment `VERSION` in `idle_compactor.py` and `launcher.py`, and the version in `release.json`. Use a new numeric version for every release, including a release that restores older code.
3. Run `python3 -m unittest discover -s . -v` and perform live read-only compatibility checks. If compaction behavior changed, distinguish simulated checks from a real accepted/completed operation.
4. Commit and push the reviewed source to the repository's default branch.
5. Publish locally with the command below. It reruns tests, builds a ZIP from an explicit file allowlist, signs its manifest, creates a draft release, uploads all assets, then publishes it as latest.

```sh
python3 publish.py --version NEXT_VERSION --out /path/to/release-output --publish
```

Use the actual next version in that command. No release or build job runs automatically on GitHub. The CLI needs the maintainer's GitHub login and local signing key. Never put the private signing key in this repository, a release, or a friend's installation.

The publisher key on the original maintainer Mac is stored at:

`~/Library/Application Support/Codex Idle Compactor Publisher/release-signing.pem`

Back up that private key securely. Public clients pin `release-public.pem`. Losing the signing key prevents transparent future updates with that trust key; rotating trust would need a planned signed transition while the original key remains available.

## Fix rollout and recovery

- New builds with compatible protocol declarations work without a utility release.
- Unknown mutation contracts or task state pause compaction, while the separate updater remains available.
- A newer signed release repairs the adapter on the next successful update check.
- The running worker gets a graceful restart request; the manager waits for it to finish its current scan/compaction before switching.
- Startup/self-check failures do not replace a healthy installed release. Early manager/worker crashes roll back locally to the prior release when one exists.
- A rolled-back version is rejected locally. Republish corrected code as a new version; do not overwrite an existing release asset.
- The release sequence prevents a stale signed manifest from silently downgrading an installation. To intentionally ship older implementation code, assign it a new version/sequence and sign it normally.

A new app feature can be unsupported until an adapter exists. Automatic updating distributes a fix; it cannot manufacture an API for ordinary ChatGPT conversations or promise compatibility before that interface is understood.

Version 0.2.6 migrates the exact old default window (1,500/1,740 seconds) on config read to 1,200/1,500 seconds. Preserve custom settings and pause/approval state. Timing metadata and usage observations live in additive ledger tables so a rollback can still read the original attempts. Keep missing counters unknown and local transcript content out of observations. Passive collection is bounded to four attempts per scan, with a five-minute per-attempt refresh interval and a 30-day background lookback; it adds no model requests.

## Hosting and cost review

GitHub's [release documentation](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases) specifies no total release-size or release-bandwidth quota. This project uses release assets, not Git LFS, GitHub Packages, or Actions artifacts. Normal hosting compute consumption is zero; there are no cron jobs or CI workflows. A client checks one manifest about 1,440 times per 30-day month while continuously awake and downloads the package only for a new version. The client enforces a 5 MiB compressed package cap, a 20 MiB extracted cap, bounded redirects, and request deadlines.

The 50/day ceiling and no monthly cap reflect the owner's explicit instructions; they are not a cache guarantee. The daily cap permits at most 1,500 utility attempts in a 30-day month, before per-turn deduplication and timing exclusions. It bounds requests, not model tokens or subscription quota, and the owner has explicitly authorized this metered path. Updating software does not enlarge those caps, reset the ledger, turn a paused installation back on, enable paid overages, or change the account's plan.
