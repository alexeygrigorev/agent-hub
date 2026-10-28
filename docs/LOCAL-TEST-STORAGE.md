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

The default Python TMPDIR remains relative to avoid long Unix socket paths.
The browser phase receives the absolute path to that same owned directory.
Keep supplied Python paths short. Persistent build output, browser evidence,
shared dependency installations, engine session history, and existing test
scratch are outside this temporary-file retention policy.

Python discovery clears every inherited `APLEXER_*`, `HUB_*`, and `AGENT_HUB_*`
variable in its child environment, along with XDG config/data/state/runtime and
GitHub config overrides. A disposable HOME alone cannot isolate the worker
profile selected by a native session tag. The parent keeps its credentials.

Vitest discovery defaults its libuv, Go/esbuild and V8 background pools to one
thread (`UV_THREADPOOL_SIZE=1`, `GOMAXPROCS=1`, `--v8-pool-size=1`). Serial test
files alone do not bound these pools: competing runtime threads can exhaust a
small CPU quota and increase wall-clock tails. Explicit caller pool settings
are preserved; other Node options are kept. These defaults apply only to the
Vitest command. Console timing reports remain visible for passing tests too.
No file is excluded, no sample is retried and the 100 ms threshold is unchanged.
