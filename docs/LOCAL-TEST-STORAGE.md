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

The default TMPDIR remains relative to avoid long Unix socket paths in browser
tests. Keep supplied paths short. Persistent build output, browser evidence,
shared dependency installations, engine session history, and existing test
scratch are outside this temporary-file retention policy.
