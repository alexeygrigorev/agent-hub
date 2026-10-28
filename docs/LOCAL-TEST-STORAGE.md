# Local test storage

Run `bash scripts/test-full-local.sh` in an isolated checkout. The runner reuses
the canonical checkout's installed Node dependencies when `package.json` and
`package-lock.json` match byte for byte. It checks npm's installed lock, package
versions and required executables before creating a `node_modules` symlink.
An existing installation is validated and preserved. Missing or mismatched
dependencies fail before tests; the runner never installs or replaces them.

To choose another checkout explicitly:

```sh
python3 scripts/local-dependencies.py --source /path/to/matching/checkout
bash scripts/test-full-local.sh
```

Do not run `npm ci` in every new worktree. One dependency installation can occupy
hundreds of megabytes. Install dependencies deliberately in a matching donor
checkout when changing the lockfile. Do not modify that donor's dependencies
while another checkout uses them. Existing duplicate installations need owner
approval before removal; the helper never removes them.

Each full test run creates a unique temporary directory below `.local/tmp`
(or the supplied `TMPDIR`). Its disposable HOME and pytest base directory live
inside that directory. Pytest retains zero temporary runs, and the shell removes
only the current run's new temporary directory on normal exit, failure, INT or
TERM. Other runs and pre-existing scratch remain untouched. SIGKILL or host
failure can leave temporary files; inspect those separately before cleanup.

The uv cache uses the original HOME/XDG cache location, or explicit
`UV_CACHE_DIR`, even while tests use a disposable HOME. This avoids downloading
a new cache into each disposable home. Vitest uses `npx --no-install` so a
missing binary cannot silently download another installation.

The default TMPDIR stays relative during Python tests to avoid long Unix socket
paths. Browser subprocesses receive the absolute path to the same owned per-run
directory: Chromium can open shared-memory files after changing its working
directory. Keep supplied paths short for Python socket fixtures. Persistent
build output, browser evidence,
shared dependency installations, engine session history, and existing test
scratch are outside this temporary-file retention policy.

Python discovery clears every inherited `APLEXER_*`, `HUB_*`, and `AGENT_HUB_*`
variable in its child environment, along with XDG config/data/state/runtime and
GitHub config overrides. A disposable
HOME alone cannot isolate the per-launch worker profile selected by a native
session tag. The parent session keeps its identity and credentials.

With a managed HOME, point `PLAYWRIGHT_BROWSERS_PATH` at an existing matching
browser installation before running the suite. The runner does not download one.

The retained-history performance test takes one fixed population of 100 samples
per case, including every sample and keeping concurrent cohorts at four requests.
Nearest-rank p95 must remain below 100 ms; six slow samples out of 100 fail. There
are no retries or discarded warm-up samples. Logs include p50, p95, maximum and
the first 20 samples' p95 for comparison. The fixture retains 500 tasks, 2,000
sessions and the blocked-work/claim cases.

For bounded verification on a shared Linux host:

```sh
systemd-run --user --scope -p CPUQuota=200% -p CPUQuotaPeriodSec=10ms \
  -p MemoryMax=1500M bash scripts/test-full-local.sh
```

Wall-clock timing includes scheduling, authentication and Durable Object queueing.
Retain failures alongside source SHA, cgroup `cpu.stat`/`cpu.pressure` and host
CPU pressure when diagnosing contention. A passing repeat does not replace a
failed full run; record the terminal result of every run without exclusions.
