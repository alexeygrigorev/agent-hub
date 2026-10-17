# Review: Windows Conversation Composition — factual audit of `3888a439f9e32d5a075a7e3d23b08ae9aae8201d`

- **Reviewed source commit**: `3888a439f9e32d5a075a7e3d23b08ae9aae8201d`
- **Direct parent**: `a42ea0fd6daeb04a1bfc4de73478f557dcf02171`
- **Published `main` (CFB source)**: `7929a1bc07bfea3a2b387989800cf1b0ba140984`
- **Original immutable report commit**: `0da55c0789c3bdfa389a7253c250e0676bfd7da8` (`docs/reviews/WINDOWS-INTEGRATION.md` blob `ff14c1b12a74b452d9b2f8013ea488886cece7ab`)
- **This auditor**: `platform-win-factual-audit` / `grok-4.6` (session `8d537279-aaa2-4d52-9963-d4fab1bbf5a3`)
- **Prior Argon execution (not this audit)**: `platform-win-merge-review` `47b53dcb-79e7-431b-b2a7-5bb02e2cdc37` / `antigravity` / `gemini-4-argon-high` on reconstructed candidate, report `0da55c0789c3bdfa389a7253c250e0676bfd7da8`
- **Author of `3888`**: `platform` / `GPT-6`
- **Review scope**: Exact tracked Windows delta on published CFB `main`; Git-object comparison to original manifests; audit of `0da` claims against source and retained runtime/probe evidence; focused POSIX/host re-check. No `--native`, no journal mutation, no source edits.
- **Verdict**: **SCOPED ACCEPT** of source `3888a439f9e32d5a075a7e3d23b08ae9aae8201d`. Physical Windows DLL/NTFS/Win35/`M3b`/`M11` remain **UNPROVEN**. The `0da` narrative is not an accurate description of this source.

The `0da55c0789c3bdfa389a7253c250e0676bfd7da8` commit stays in Git unchanged. This file is a separate corrective audit identity on parent `3888a439f9e32d5a075a7e3d23b08ae9aae8201d`.

---

## 1. What this audit reviewed

`3888a439f9e32d5a075a7e3d23b08ae9aae8201d` applies the byte-identical reviewed Windows patch (`bf2f3df7070c2c9145ca86dd4ada7e5ff6399c6e478284807e0d1009b5d61500`) onto published CFB `main` `7929a1bc07bfea3a2b387989800cf1b0ba140984`. The ten Windows paths are already tracked here. This is not an inert reconstructed candidate.

Compared read-only:

- Original composed manifest `/home/alexey/git/agent-hub-wt-platform-win-merge-review/.local/review/composed-output-manifest.json` (203 paths, compact SHA-256 `5fb8eeace9f7b936e1b9043902cea3783cd0aa72decabedb51f96bef298e8804`)
- Original CFB input manifest `.../cfb-input-manifest.json` (199 paths, compact SHA-256 `17aab235373ff0f98cce9ff361b74333d9d3bd23bf45790d471865f3ea02223a`)
- Git blobs at `3888a439f9e32d5a075a7e3d23b08ae9aae8201d` and `7929a1bc07bfea3a2b387989800cf1b0ba140984`
- Proposal table in `docs/proposals/WINDOWS-INTEGRATION.md` (correct paths/hashes)
- Prior retained probe/runtime evidence under that reviewer `.local/review` and head `.local/platform/win-merge-review-native-reconciliation.json`
- Tracked source `host/agent_hub_host/{private_fs,win32_fs,config,bridge,conversation_bridge,native_bridge}.py` and tests

Source, tests, and proposals were not modified. Owned output is this report plus gitignored `.local/audit` / `.local/tmp` (combined cap 20 MiB).

---

## 2. Exact Git pins

| Object | Full SHA | Role |
| :--- | :--- | :--- |
| Reviewed source | `3888a439f9e32d5a075a7e3d23b08ae9aae8201d` | Windows delta on published CFB `main` |
| Direct parent | `a42ea0fd6daeb04a1bfc4de73478f557dcf02171` | Proposal + retained evidence docs |
| Published `main` | `7929a1bc07bfea3a2b387989800cf1b0ba140984` | CFB 199 source files + 3 conversation proposal/review docs |
| CFB freeze | `cfb4dc26985cbe39a1ae770ade4a6a57880edcbc` | Parent `f9f6fb258e5abdf4d97e9292043828b4cc80ac82` |
| CFB independent review | `5a4b9381d4863d10a1d46b249c0be83e17cee18f` | `docs/reviews/CONVERSATION-INTEGRATION.md` |
| Standalone Windows proposal | `2de0a6c1846b110227cd1d15e99a7af6999158f5` | Patch source of `bf2` |
| Standalone Windows review | `3d0dfadbed35812aeaacbb98f304acfecf711e91` | Prior scoped accept of the same patch |
| Original composition report | `0da55c0789c3bdfa389a7253c250e0676bfd7da8` | Immutable; 133-file report commit on `d79fab0175fc57a0909da41d8f5d9f5ef4c0bba9` |
| `d79fab0175fc57a0909da41d8f5d9f5ef4c0bba9` | 133 tracked files | Report-era worktree; **not** the 203-file composed source |

| Digest | Value | Status |
| :--- | :--- | :--- |
| Windows patch SHA-256 | `bf2f3df7070c2c9145ca86dd4ada7e5ff6399c6e478284807e0d1009b5d61500` | Identical at `2de0a6c`, `a42ea0f`, and `3888` |
| CFB patch SHA-256 | `e8c91a9c3f3a795360c85eec300d3df9187a361d57f9a9dfc9312ad60e673d10` | Unchanged |
| CFB 199-file compact JSON | `17aab235373ff0f98cce9ff361b74333d9d3bd23bf45790d471865f3ea02223a` | All 199 CFB paths on `7929` match |
| Composed 203-file compact JSON | `5fb8eeace9f7b936e1b9043902cea3783cd0aa72decabedb51f96bef298e8804` | All 203 paths in `3888` match original manifest |
| Non-Windows CFB paths | 193 | Byte-identical `7929` → `3888` |

Tracked counts: `7929` has 202 files (199 CFB source + 3 conversation docs). `3888` has 209 files (203 source + those 3 conversation docs + Windows proposal/patch/review docs). Compact JSON excludes those extra docs.

### Ten Windows paths (Git blobs at `3888` = original composed manifest)

| Path | `7929` SHA-256 | `3888` / composed SHA-256 |
| :--- | :--- | :--- |
| `host/agent_hub_host/bridge.py` | `2b5e775dbbf194fcb619171a483b9b4152af8a2e76c46a6450b47ead8f9eb464` | `033db99502033296143522917d01bcf63073f41cd0e3bf4f32227187a017b5ea` |
| `host/agent_hub_host/config.py` | `2ee1abe6c62cf93d494cb0098525d69750f462cb2ec5024a8b5cacd8a8e29537` | `6cce08e8682e4b70b3d26231716dc1ceb4c041f618fd4fb7ecf13212e38999fa` |
| `host/agent_hub_host/conversation_bridge.py` | `7e7ebaec3148b4cb5522bdead67e171f9adbcbb8e6c0d94085813c80ee313a4e` | `747e3cf7ccd2296f89cd833c7a6864dfce7602f79bf50095e6b17cc3297b5855` |
| `host/agent_hub_host/native_bridge.py` | `b8f1f4dbac0093f48176542fad393342866c07348b7728198ec643ab0fce0b3f` | `528dab707ebd7ae12eb97d7db94a6cf17658108dfd1e16f7d1c77e89c5fc8ab9` |
| `host/agent_hub_host/private_fs.py` | absent | `ef4b6436bb74c08b2e4e367f82bf5a02bd1db9e3d04f416b480b46f8c8843a18` |
| `host/agent_hub_host/win32_fs.py` | absent | `34589f69dc0f11cabb79fd08afaf0c4a48b3a7e6ee2c2bf549b7066ade99c302` |
| `host/tests/test_bridge.py` | `783d38efeac5047c0c0182d073ef4e3b553526c3c4c29364c82531ecdecf51e5` | `dcb6dd44cd21753ef9e773df5aefc8ed2b60e30268cbdbff23444bbc73bf237c` |
| `host/tests/test_config.py` | `7d6fe1beab39bb10409f829458e1c7280be78870bb03f74c32b9859dc9940ab8` | `761b35077d66fc2c541de0d03409c693fd1ee65d0b17a85d8f1d1eef830c9399` |
| `host/tests/test_private_fs.py` | absent | `bd56b0cf54df9a92a39430754e6bc15e6a1f7fc1e5cac580bd94dad3529cc1bd` |
| `host/tests/test_windows_imports.py` | absent | `8a7d5bb02994c0558c13843f62823eb9d89c0e3eac5431e138c3e9291b06c52b` |

---

## 3. Factual errors in `0da55c0789c3bdfa389a7253c250e0676bfd7da8`

These are report defects. They are not evidence that `3888` source is a different verbose implementation, and they are not harmless typos. Prior probe scripts and the original composed manifest already used the compact `agent_hub_host` APIs and the hashes in section 2.

### 3.1 Nonexistent module `host/hub_agent`

`0da` describes `host/hub_agent/{bridge,config,conversation_bridge,native_bridge,private_fs,win32_fs}.py` and `from hub_agent.private_fs import ...`.

Git at `3888`, `7929`, and `d79fab` has **zero** `host/hub_agent/` paths. Actual package is `host/agent_hub_host/` (27 files at `3888`). Relative imports are `from . import private_fs`. Tests use `from agent_hub_host import private_fs as fs, win32_fs as win`. The original composed-output-manifest.json already listed `host/agent_hub_host/...`.

### 3.2 Invented pin `f9f6fb21495763a025610878a7f0606b6a0c8d71`

That object does not exist in this repository. The real `f9f6fb2` commit is `f9f6fb258e5abdf4d97e9292043828b4cc80ac82` (`docs: prepare combined conversation and fleet UI checkpoint`). It is the parent of CFB freeze `cfb4dc26985cbe39a1ae770ade4a6a57880edcbc`. Published CFB source is `7929a1bc07bfea3a2b387989800cf1b0ba140984`. Treating the invented SHA as CFB semantics is false.

### 3.3 `MAX_JOURNAL_BYTES` versus `MAX_STATE`

No `MAX_JOURNAL_BYTES` symbol exists in `3888` Python. `host/agent_hub_host/conversation_bridge.py` defines `MAX_STATE = 8 * 1024 * 1024` and uses it for `read_bytes` / `atomic_write`. Oversize raises `ValueError('Private file exceeds byte bound')` or `BridgeCapabilityGap('Journal full; retain state and reconcile before continuing')`.

### 3.4 Invented 10-path hash table

None of the `0da` input/output SHA-256 values appear in the original composed-output-manifest.json or in `3888` Git blobs. Example: `0da` gives composed `private_fs.py` as `dfce9a087026e97d6ca0977e0037e938a3918b35e7b2bf6d3448a5b942b870a2`; actual is `ef4b6436bb74c08b2e4e367f82bf5a02bd1db9e3d04f416b480b46f8c8843a18`. The proposal table in `docs/proposals/WINDOWS-INTEGRATION.md` already had the correct hashes.

### 3.5 Pairing `eee4` / `1149` conflation, and `0896` as pairing

| Commit | Subject |
| :--- | :--- |
| `1149f65394611d05c1f55fcec4e803f105930c8d` | `docs: finalize tested public machine-bound agent pairing proposal` |
| `eee4d2f88305a6e64545c8e7b68f329a162811d1` | `Record fresh pairing repair validation and bounded evidence` |
| `0896ebe157cc732342b17fb40a6cf6e096ef93a2` | `fix(cli): keep Unix bridge imports lazy for unrelated commands` |

`eee4` is not `1149`. `0896` is CLI import laziness / gap-recovery host plumbing, not pairing. Automatic machine binding in this source is `config.record_authenticated_machine_binding` + `update_config` / `save_config`, including `None` `machine_id` (no fabricated binding) and conflict refusal (`Conflicting machine_id for paired host config`). There is no `_persist_authoritative_machine_id`.

### 3.6 `d79fab` is not the composed source

`d79fab0175fc57a0909da41d8f5d9f5ef4c0bba9` has 133 tracked files and does not contain `private_fs.py` or `win32_fs.py`. It is the parent of the `0da` report on older `main`. The 203-file composition lived in that reviewer's private reconstruction. Tracked composition is `3888`.

### 3.7 Invented APIs and CLI names

Absent from `3888` source: `validate_private_parent`, `open_no_follow`, `validate_open_regular_fd`, `atomic_write_bytes`, `PrivateJsonFile`, `ConversationJournal`, `_posix_parent_dir_fd`, `_persist_authoritative_machine_id`, `JOURNAL_VERSION`, `_state_dir_candidates`, `_binary_names`, `_apply_owner_only_dacl`, `run_conversation_bridge`, `a.exe` / `aplexer.exe` helpers.

Actual POSIX/Windows storage API: `parent`, `_open`, `_private`, `read_bytes`, `atomic_write`, `exclusive_lock`. Journal type is `ConversationBridge` with `journal()`, `cycle()`, `resume()`, `run_cycles()`, `resume_from_args()`. Journal JSON has `version: 1` (no `JOURNAL_VERSION` constant). Staging name is `path.name + '.' + uuid.uuid4().hex + '.tmp'` (example `journal.json.<32hex>.tmp`), not `.{name}.{secrets.token_hex(16)}.tmp`.

Prior probe file `.local/review/test_reviewer_probes.py` already imported `agent_hub_host` and called `private_fs.atomic_write` / `ConversationBridge`. `0da` section 7.2 rewrote those probes into the invented names. The probe log (`6/6` PASS) matches the probe file, not the report narrative.

### 3.8 Win32 apply/verify mix-up and extra DLL

`win32_fs.py` loads `kernel32` and `advapi32` only inside `API.__init__` (`api()`). It does not load `ucrtbase.dll`. CRT use is CPython `msvcrt.open_osfhandle` / `msvcrt.locking`.

DACL **application** is `ConvertStringSecurityDescriptorToSecurityDescriptorW` with `O:{sid}D:P(A;{OICI};FA;;;{sid})` passed as `SECURITY_ATTRIBUTES` to `CreateFileW` / `CreateDirectoryW`. There is no `SetSecurityInfo` in this file. DACL **verification** is `GetSecurityInfo` + `GetSecurityDescriptorControl` + `GetAce` + `EqualSid`. `SE_DACL_PROTECTED` (`0x1000`) is required when `protected=True`. `acl.count` must be **exactly 1**; the report's "every ACE" wording is weaker than the source.

`GetFinalPathNameByHandleW` is used only in `canonical_workspace()`. Ancestor pinning is `parent()` opening each component with `CreateFileW` flags `0x00200000` (`FILE_FLAG_OPEN_REPARSE_POINT`) and, for directories, `0x02000000` (`FILE_FLAG_BACKUP_SEMANTICS`). `info()` refuses `FILE_ATTRIBUTE_REPARSE_POINT` (`0x400`) and wrong directory/file kind. Share mode is `3` (`FILE_SHARE_READ|FILE_SHARE_WRITE`, no delete). Replace flags are `1|8` (`MOVEFILE_REPLACE_EXISTING|MOVEFILE_WRITE_THROUGH`). There is no `FlushFileBuffers` call; durability is `os.fsync` on the CRT fd before replace, plus `WRITE_THROUGH` on Windows.

`ctypes.sizeof(FileInfo) == 52` and `ACE.sid.offset == 8` are true. Field names in source are shortened (`attributes`, `times`, `volume`, `sid`), not the Microsoft `dwFileAttributes` / `SidStart` identifiers used in `0da`.

`local_path()` requires a plain absolute local drive path, `GetDriveTypeW == 3` (fixed), filesystem name `NTFS`. UNC, `\\?\`, ADS (`:` in tail), `..`, reserved device names, and forward slashes are refused.

### 3.9 POSIX flag and lock claims that the source does not implement

`0da` claimed `O_CLOEXEC`, `fchmod(fd, 0o600)`, `(st_dev, st_ino)` match after open, post-lock inode re-stat, and `st_mode & 0o177 == 0`.

Actual POSIX `parent()` uses `O_RDONLY|O_DIRECTORY|O_NOFOLLOW`. `_open()` uses `O_NOFOLLOW|O_NONBLOCK` and create mode `0o600`. `_private()` checks owner UID via `os.getuid()` (not `geteuid()`), `st_mode & 0o077 == 0`, directory vs regular, and `st_nlink == 1` for files. Owner-execute (`0o100`) is not rejected. There is no `(dev,ino)` cross-check and no post-`flock` re-stat. `exclusive_lock` never unlinks the lock file.

`ConversationBridge.journal()` maps `PermissionError`/`ValueError` to `BridgeCapabilityGap('Unsafe bridge journal or file safety proof failed')` and `OSError` errno 20/40 to `Unsafe bridge journal path`. Lock contention surfaces as `BlockingIOError`. `0da` claimed a `BridgeCapabilityGap("Conversation bridge is already running for this journal")` wrapper; prior probe 03 asserts `BlockingIOError`.

### 3.10 Config / native path claims

`config.get_config_path()` on Windows requires `APPDATA` and uses `APPDATA/agent-hub/host.json`. It does not consult `LOCALAPPDATA` or `USERPROFILE`. Native state/runtime roots in `bridge.py` use `LOCALAPPDATA/aplexer/{state,run}` when `os.name == 'nt'`. No `_binary_names()` / `a.exe` helper exists in this delta.

### 3.11 HOLD / gap control flow

Disabled `run_cycles(..., enabled=False)` raises `BridgeCapabilityGap(HOLD_REASON)`. Contiguous-history failures raise `BridgeCapabilityGap('History is not contiguous/canonical')`. Successful `cycle()` returns `{'status': 'completed', ...}`. Successful `resume()` returns `{'status': 'gap_recorded', 'server_read_advanced': False, ...}`. There is no `status="HOLD"` return from `run_conversation_bridge`.

### 3.12 Line numbers and the Windows reparse test

`private_fs.py` is 137 lines. Lines 63–72 are `_open`/`read_bytes`, not `validate_private_parent`. `test_real_windows_reparse_refused_if_symlink_setup_supported` is `host/tests/test_private_fs.py` lines 77–89, not 198–236.

That one test, when it runs on Windows, creates a directory symlink to the journal parent and asserts `read_bytes` through the link raises `OSError`. Privilege error `winerror == 1314` skips. It does not exercise owner-only DACL application, `MoveFileExW`, or `msvcrt.locking`. Claiming it covers ACL/atomic locks overstates the test.

---

## 4. Actual source contract (what `3888` does)

### 4.1 `host/agent_hub_host/private_fs.py`

POSIX: walk from the path anchor with directory `O_NOFOLLOW` descriptors; optional `mkdir(..., 0o700)` when `create=True`; require the immediate parent to be an owner-only directory; open the leaf `O_NOFOLLOW` as a single-link regular file. `atomic_write` stages an exclusive sibling `*.<32hex>.tmp`, `fsync`s, `os.replace`s (or `win32_fs.replace` on Windows), and unlinks residue. `exclusive_lock` uses `fcntl.flock(LOCK_EX|LOCK_NB)` on POSIX and `msvcrt.locking(LK_NBLCK, 1)` after `lseek(0)` on Windows; the kernel releases the lock on close/process death; the lock inode is not unlinked.

Windows dispatch is `sys.platform == 'win32'` via `win32_fs.parent` / `open_file` / `replace`.

### 4.2 `host/agent_hub_host/win32_fs.py`

Source contract tests import this module on POSIX; `api()` is the only loader of Windows DLLs. Primary references for constants actually used:

- [BY_HANDLE_FILE_INFORMATION](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/ns-fileapi-by_handle_file_information) / [GetFileInformationByHandle](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfileinformationbyhandle) — 52-byte layout verified in-process
- [CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew) — `GENERIC_READ` `0x80000000`, `GENERIC_WRITE` `0x40000000`, `READ_CONTROL` `0x20000`, share `3`, `CREATE_NEW=1` / `OPEN_EXISTING=3` / `OPEN_ALWAYS=4`, `FILE_FLAG_OPEN_REPARSE_POINT` `0x00200000`, `FILE_FLAG_BACKUP_SEMANTICS` `0x02000000`
- [GetFinalPathNameByHandleW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfinalpathnamebyhandlew) — canonical workspace only
- [GetSecurityInfo](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-getsecurityinfo), [GetSecurityDescriptorControl](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-getsecuritydescriptorcontrol), [ConvertStringSecurityDescriptorToSecurityDescriptorW](https://learn.microsoft.com/en-us/windows/win32/api/sddl/nf-sddl-convertstringsecuritydescriptortosecuritydescriptorw)
- [MoveFileExW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw)
- [msvcrt](https://docs.python.org/3/library/msvcrt.html) — `open_osfhandle` transfers HANDLE ownership to the CRT fd; `LK_NBLCK=2`

This is a source/ABI reading. It is not physical execution of those DLLs on NTFS.

### 4.3 Composition into config / bridge / conversation / native

- `config.save_config` / `load_config` use `private_fs.atomic_write` / `read_bytes` with a 1 MiB bound. Windows config root is `APPDATA`.
- `bridge.py` drops Unix-only top-level `fcntl`; receipts use `private_fs.atomic_write`; native roots on Windows are `LOCALAPPDATA/aplexer/{state,run}`; `canonicalize_workspace` delegates to `win32_fs` on `nt`.
- `conversation_bridge.py` journals under `<workspace>/.local/conversation-bridge-<digest([actor,project])[:24]>.json`, identity 7-tuple `[origin, actor, project, workspace, machine, session_id, tag]`, `member(cid)` before native/Hub effects, pending delivery/reply intents, P3 `resume(..., acknowledge_gap=True)` with empty-range proof via ascending `after_seq`, atomic `gaps`+`cursors` write, `server_read_advanced=False`, equal-target replay refused.
- `native_bridge.py` reads session records through `private_fs.read_bytes(..., private=False)` so Windows native-registry inherited ACLs can be verified without rewriting them (`win32_fs.parent(..., native=True)`).

### 4.4 `.local` `0700` POSIX precondition

`_private` on a directory rejects group/world bits. Existing workspaces whose `.local` is `0775`/`0755` fail closed. Owner handoff is metadata-only `chmod 0700 <workspace>/.local` by that workspace owner; not recursive; journals are not moved or reset. This audit did not migrate any `.local` outside its own `.local/audit` and `.local/tmp`.

---

## 5. Evidence

### 5.1 This audit (tracked `3888` worktree)

Serial, `taskset -c 0`, `umask 077`, `TMPDIR` under `.local/tmp`, `UV_THREADPOOL_SIZE=1`, `NODE_OPTIONS=--v8-pool-size=1`, `GOMAXPROCS=1`, `PYTHONDONTWRITEBYTECODE=1`. No installs; no `--native`; no journal writes in this workspace.

| Check | Result | Evidence |
| :--- | :--- | :--- |
| Git-object 203-file compact JSON | `5fb8eeace9f7b936e1b9043902cea3783cd0aa72decabedb51f96bef298e8804` | `.local/audit/manifest-compare.json` |
| CFB 199 on `7929` | `17aab235373ff0f98cce9ff361b74333d9d3bd23bf45790d471865f3ea02223a`; 193 unchanged into `3888` | same |
| Host `py_compile` | **58/58** | `.local/audit/focused-checks.json` |
| `host/tests/test_private_fs.py` | 15 PASS + **1 SKIP** `test_real_windows_reparse_refused_if_symlink_setup_supported` (`Actual Windows reparse evidence only`) | `.local/audit/focused-unittest.log` |
| `host/tests/test_windows_imports.py` | 3/3 PASS (fcntl-free import/CLI help, Windows `LOCALAPPDATA` roots, automatic machine binding) | same |
| `host/tests/test_config.py` | 7/7 PASS | same |
| `host/tests/test_conversation_bridge_gap.py` | 24/24 PASS (run from `host/tests` so `import test_conversation_bridge as base` resolves) | `.local/audit/gap-unittest.log` |
| In-process ABI sizes | `sizeof(FileInfo)==52`, `ACE.sid.offset==8`, `MAX_STATE==8388608`; invented names absent | `.local/audit/focused-checks.json` |

A first unittest invocation of `test_conversation_bridge_gap` as `host.tests.*` failed to import (`ModuleNotFoundError: test_conversation_bridge`). That was this runner's module path, not a `3888` defect. Re-run from `host/tests` passed 24/24. The failed log is retained.

Scratch for `.local/audit` + `.local/tmp`: peak allocated 28672 bytes, apparent 20461 bytes, cap 20971520 bytes. A 0-byte `uv-*.lock` in `.local/tmp` was left untouched.

### 5.2 Prior `47b53dcb` execution, reused because 203 files are byte-identical

Attributed to reviewer `platform-win-merge-review` `47b53dcb-79e7-431b-b2a7-5bb02e2cdc37`. Not replayed here. Paths are in `/home/alexey/git/agent-hub-wt-platform-win-merge-review/.local/review/` unless noted.

| Check | Recorded result | Note |
| :--- | :--- | :--- |
| Host union | 654 tests: 653 PASS + 1 actual-Windows SKIP; 749 passing subtests | `host-union-counts.json`; skip reason matches this audit |
| Reviewer probes | 6/6 PASS against compact `agent_hub_host` APIs | `test_reviewer_probes.py`, `reviewer-probes.log` — **not** the invented APIs in `0da` §7.2 |
| Typecheck | PASS | `typecheck.log` |
| Composed Worker Vitest | 92/92 | `worker-composed.log` |
| Dakit lock + UI static | 21 locked files; 2/2 scripts PASS | `kit-*.log` |
| Public pairing | 9/9 groups PASS | `public-pairing.log`; read-only native qualify; 0 journal writes claimed |
| Bundle dry-run | PASS `552.38 KiB` / `105.98 KiB` gzip | `bundle.log` |
| Own-session native POSIX + P3 | 10/10 groups; gap-first 0 mail; envelope `01a14800-8d7a-7fc0-849a-b1bd1d488509`; reply `01a14800-901f-7a01-868b-9f358abff1d0`; journal SHA-256 `8360ff91bce42ec3732efc35b9701ae6e6fcf9cd9eebf99b92d80a3b64582c87`; cursors 4 and 3; 1 delivery, 1 done reply, 1 gap, 0 pending | `native-reconciliation.json`; head read-only check `/home/alexey/git/agent-coordination/.local/platform/win-merge-review-native-reconciliation.json` |

This audit did not replay those native identities or modify those journals.

Worker/UI/pairing/native layers sit in the 193 unchanged CFB files plus the ten Windows host files already hash-verified. Broad repetition was not required for this factual audit.

---

## 6. Physical Windows owner handoff (executable, honest)

Linux source tests and the skipped reparse case are source evidence only. They do not enroll Win35, load real DLLs, or prove NTFS effects.

On a genuine Windows 10/11 **local NTFS** host, at exact `3888a439f9e32d5a075a7e3d23b08ae9aae8201d`, with project-local Python (no new global install):

```text
python -m unittest discover -s host/tests -v
```

Required observations:

1. `test_real_windows_reparse_refused_if_symlink_setup_supported` **runs**. A skip for `winerror 1314` is a fixture/privilege skip, not a pass. If it runs, it only proves `read_bytes` refuses a directory symlink to the journal parent. Record whether the fixture was a symlink, a junction, or could not be created.
2. `test_real_subprocess_contention_and_process_death_release` **PASS** — this is the real lock-contention / process-death test (uses `msvcrt.locking` on `nt`). It is separate from the reparse test.
3. `test_positive_roundtrip_replacement_and_bounds` **PASS** — real atomic replace/bounds on that volume.
4. `test_hardlinks_rejected_for_reads_writes_and_locks` **PASS** if NTFS hardlinks can be created.
5. `SimulatedWindowsTests` remain **mocks** even on Windows (`patch.object(win, 'api', ...)`). Passing them is not physical ACL/NTFS proof.
6. `test_windows_imports` and `test_config` PASS for portable imports and `APPDATA` / `LOCALAPPDATA` roots.

Still **UNPROVEN** until a Windows owner records them on that host:

- Actual `kernel32.dll` / `advapi32.dll` load via `win32_fs.api()`
- Real owner-only protected DACL on created journals (`O:{sid}D:P(A;;FA;;;{sid})` files / `OICI` directories), inspected with OS tools, not FakeAPI
- Junction vs symlink vs mount-point vs ADS vs UNC vs non-NTFS fail-closed on real volumes
- `MoveFileExW` sharing/ACL failure behavior beyond the unit mock
- Public device login, enrolled native identity, bidirectional hub↔native messages, offline/reconnect dedup, reply and gap reconciliation on that machine
- Win35 fleet enrollment and `M3b`/`M11` physical acceptance

Keep Windows root stood down. Do not treat this scoped source accept as host adoption.

---

## 7. What remains separate

- `main` integration of `3888` (head owns the push)
- Live runtime rollout
- `GATE-CONV-REVIEW`
- Roadmap `M4`, `M5`, `M7`, `M8a`, `M8b`, `M9`, `M10`, `M11`
- POSIX `.local` `0700` migration of existing shared workspaces (owner ACK required)

No blocking source defect was found in `3888a439f9e32d5a075a7e3d23b08ae9aae8201d` that requires a product patch before scoped source accept. Compact APIs, hashes, and P3/public-CLI tests match the independently executed composition. `0da` cannot be used as the ABI/security description of that source.

---

## 8. Verdict

**SCOPED ACCEPT** `sha=3888a439f9e32d5a075a7e3d23b08ae9aae8201d` `reviewer=platform-win-factual-audit/grok-4.6`

Original report record (immutable, superseded as description): `0da55c0789c3bdfa389a7253c250e0676bfd7da8` blob `ff14c1b12a74b452d9b2f8013ea488886cece7ab`.
