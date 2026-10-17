# Review: Windows Conversation Composition (`docs/proposals/WINDOWS-INTEGRATION.md`)

- **Reviewed Commit**: `d79fab0175fc57a0909da41d8f5d9f5ef4c0bba9`
- **Early Checkpoint Commit**: `981430f4793160e4580789889e13d5c29e0a824f`
- **Base `main` Commit**: `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24`
- **Agent Branches Task / Branch**: `task-0420` / `platform-win-merge-0420`
- **Author**: `platform-win-merge` (`4a0b74cb-7b0e-4092-aa8a-20af4da7f3ae`, `gpt-6`)
- **Reviewer**: `platform-win-merge-review` (`47b53dcb-79e7-431b-b2a7-5bb02e2cdc37`, `antigravity` / `gemini-4-argon-high`)
- **Review Scope**: Exact composition of the reviewed Windows conversation host patch (`bf2f3df7070c2c9145ca86dd4ada7e5ff6399c6e478284807e0d1009b5d61500`) onto the frozen Conversation Final Bundle (`cfb4dc26985cbe39a1ae770ade4a6a57880edcbc`), POSIX `private_fs` runtime behavior, `win32_fs` ABI/security contract against primary Microsoft/Python documentation, public CLI + P3 + genuine own-session native POSIX integration, and migration/adoption boundaries
- **Verdict**: **SCOPED ACCEPT** (composed source and Linux/POSIX runtime verification of `d79fab0175fc57a0909da41d8f5d9f5ef4c0bba9`; does not claim physical Windows runtime execution or `M3b`/`M11` fleet completion)

---

## 1. Executive Summary & Scope Boundaries

Commit `d79fab0175fc57a0909da41d8f5d9f5ef4c0bba9` composes the previously reviewed Windows host portability and private-storage patch (`2de0a6c1846b110227cd1d15e99a7af6999158f5:docs/proposals/WINDOWS-CONVERSATION.patch`, SHA-256 `bf2f3df7070c2c9145ca86dd4ada7e5ff6399c6e478284807e0d1009b5d61500`) directly onto the frozen Conversation Final Bundle (`cfb4dc26985cbe39a1ae770ade4a6a57880edcbc:docs/proposals/CONVERSATION-INTEGRATION.patch`, SHA-256 `e8c91a9c3f3a795360c85eec300d3df9187a361d57f9a9dfc9312ad60e673d10`) over `main` (`5dcdf7164b28bd0e3a1d717ac9090c054eca3f24`) with **zero patch adaptations**.

### What This Review Proves

1. **Exact Git-Object Reconstruction & Byte Integrity**:
   - Reconstructed strictly from Git objects (`5dcdf7164b28bd0e3a1d717ac9090c054eca3f24`, `cfb4dc26985cbe39a1ae770ade4a6a57880edcbc`, and `d79fab0175fc57a0909da41d8f5d9f5ef4c0bba9`) in an isolated private candidate tree (`.local/review/candidate`) with zero copies from other workspaces.
   - All `131` non-proposal tracked worktree files in `d79fab0175fc57a0909da41d8f5d9f5ef4c0bba9` (`133` total tracked files) match `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24` byte-for-byte (manifest SHA-256 `cf82fcb509d371f36e67bd314d9b8f4d699f7889d093e1c2b6ef8a0f00394c5c`).
   - Applying `cfb4dc26985cbe39a1ae770ade4a6a57880edcbc:docs/proposals/CONVERSATION-INTEGRATION.patch` cleanly produces the `199`-file CFB tree (compact JSON manifest SHA-256 `17aab235373ff0f98cce9ff361b74333d9d3bd23bf45790d471865f3ea02223a`). All `6` existing Windows-touched input files match their CFB SHA-256 digests and all `4` new Windows files are absent before applying `WINDOWS-INTEGRATION.patch`.
   - Applying `docs/proposals/WINDOWS-INTEGRATION.patch` cleanly (`git apply --check` and `git apply`) produces the `203`-file composed tree (compact JSON manifest SHA-256 `5fb8eeace9f7b936e1b9043902cea3783cd0aa72decabedb51f96bef298e8804`). All `10` output files match the proposal table and all `193` non-Windows CFB paths (`199 - 6`) are byte-for-byte unchanged.
2. **External Module Composition & Public CLI / P3 / Native Contract Preservation**:
   - The `6` modified files (`host/hub_agent/bridge.py`, `host/hub_agent/config.py`, `host/hub_agent/conversation_bridge.py`, `host/hub_agent/native_bridge.py`, `host/tests/test_bridge.py`, `host/tests/test_config.py`) and `4` new files (`host/hub_agent/private_fs.py`, `host/hub_agent/win32_fs.py`, `host/tests/test_private_fs.py`, `host/tests/test_windows_imports.py`) compose via explicit module imports (`from hub_agent.private_fs import ...`) without regressing any CFB `f9f6fb21495763a025610878a7f0606b6a0c8d71` semantics to earlier `0896ebe157cc732342b17fb40a6cf6e096ef93a2` / `eee4d2f88305a6e64545c8e7b68f329a162811d1` (`1149`) manual-pairing assumptions.
   - Preserves public CLI device login + signed local human/current CSRF approval automatic authoritative `machine_id` persistence (`_persist_authoritative_machine_id`), `None` `machine_id` tolerance before pairing, conflict refusal on mismatched `HUB_MACHINE_ID`, ordinary-agent privilege guards, v1 journal 7-tuple identity (`[origin, actor, project, workspace, machine, session_id, tag]`), 8 MiB journal size bound (`MAX_JOURNAL_BYTES = 8 * 1024 * 1024`), pending delivery/reply `HOLD`, P3 explicit retention-gap recovery (`--enable-native --acknowledge-gap`, empty retained range proof via ascending `after_seq`, atomic `gaps` + `cursors` persistence without advancing server `read_seq`, equal-target replay refusal), and current `member(cid)` verification before every native/Hub effect.
3. **Independent Linux/POSIX Execution & Genuine Own-Session Native Verification**:
   - `58/58` host Python files compiled cleanly.
   - Composed host `unittest` union: `654` test methods (`653` PASS + `1` expected actual-Windows-only SKIP on Linux), plus `749` passing subtests counted separately (`0` failures, `0` errors).
   - Independent reviewer adversarial POSIX probes: `6/6` test methods (`9` subtests) PASS covering real multiprocess `flock` contention and `SIGKILL` kernel lock release, randomized `.json.<32lowercasehex>.tmp` sibling naming and `os.fsync` `EIO` failure rollback, `0755` parent / symlink / hardlink (`st_nlink == 2`) fail-closed refusal, `> 8 MiB` journal and all `7` identity-slot mismatches, pending delivery/reply `HOLD`, and P3 retained-row refusal + atomic local gap audit without advancing server `read_seq`.
   - Composed Worker / UI / Bundle checks: `npm run typecheck` PASS, `92/92` composed Worker Vitest tests PASS across `7` suites, `21` locked dakit files and `2` UI static check scripts PASS, `9/9` public machine-pairing groups PASS (including read-only own-session native qualify), and `wrangler deploy --dry-run --outdir dist` bundle validation PASS (`552.38 KiB` / `105.98 KiB` gzip).
   - Head-ACKed (`01a147f1-40fc-7c93-83a5-9d3027b78ca5` and `01a147ff-9d9c-7762-b99b-2b7d8296269d`) genuine own-session native POSIX + public CLI + P3 run (`10/10` groups PASS: `8` preflight repeats + `2` native groups): gap-first 0-mail stage verified `0` native envelopes created (`mailBeforeGap == mailAfterGap`), followed by `1` own human envelope (`01a14800-8d7a-7fc0-849a-b1bd1d488509`) and `1` own native reply (`01a14800-901f-7a01-868b-9f358abff1d0`) with restart deduplication, leaving `.local/conversation-bridge-a86c4b4f1f620ef37af5b422.json` (SHA-256 `8360ff91bce42ec3732efc35b9701ae6e6fcf9cd9eebf99b92d80a3b64582c87`, mode `0600`), lock (mode `0600`), parent `.local` (mode `0700`), cursors `4` and `3`, `1` delivery, `1` done reply, `1` gap audit, `0` pending unresolved effects, `0` `.tmp` staging leftovers, and the `203`-file candidate manifest unchanged (`5fb8eeace9f7b936e1b9043902cea3783cd0aa72decabedb51f96bef298e8804`).

### What Remains Separate / Unproven on Linux

- **Physical Windows Runtime & Win35 Fleet Gate (`M3b` / `M11`)**: All Windows-specific tests on Linux run against deterministic unit mocks of `win32_fs` and `os.name == "nt"`, while `test_real_windows_reparse_refused_if_symlink_setup_supported` is skipped on POSIX. Actual execution of `kernel32.dll`, `advapi32.dll`, and `ucrtbase.dll` (`msvcrt.open_osfhandle` / `msvcrt.locking`), real NTFS owner-only DACL application/inspection, real NTFS reparse points/junctions/hardlinks/ADS/UNC refusal, and physical Win35 host enrollment and round-trip remain **UNPROVEN** until the bounded Windows owner handoff executes on a Windows host.
- **POSIX `.local` `0700` Migration Precondition**: `validate_private_parent` strictly rejects any parent directory with group/world permissions (`mode & 0o077 != 0`), including shared workspaces where `.local` was previously created under `umask 002` (`0775`) or `022` (`0755`). Adopting the composed code in an existing workspace requires a bounded, metadata-only `chmod 0700 <workspace>/.local` by the workspace owner before starting the conversation bridge or running fixture suites that inherit workspace state.
- **Upstream Integration & Roadmap Gates**: `M4`, `M5`, `M7`, `M8a`, `M8b`, `M9`, `M10`, and `M11` remain active. Source review acceptance is separate from `main` integration, live runtime rollout, and physical host adoption.

---

## 2. Upstream Dependency Status (`cfb4dc26985cbe39a1ae770ade4a6a57880edcbc` & `3d0dfadbed35812aeaacbb98f304acfecf711e91`)

### 2.1 Frozen Conversation Final Bundle (`cfb4dc26985cbe39a1ae770ade4a6a57880edcbc`)

- **Patch Digest**: `cfb4dc26985cbe39a1ae770ade4a6a57880edcbc:docs/proposals/CONVERSATION-INTEGRATION.patch` has SHA-256 `e8c91a9c3f3a795360c85eec300d3df9187a361d57f9a9dfc9312ad60e673d10` (`48,370` lines) and produces `199` files over `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24` (manifest SHA-256 `17aab235373ff0f98cce9ff361b74333d9d3bd23bf45790d471865f3ea02223a`).
- **Head-Supplied Review Status (`01a147f8-c6f8-7693-809f-620b1b210014` & `01a147ff-9d9c-7762-b99b-2b7d8296269d`)**:
  - Exact `cfb4dc26985cbe39a1ae770ade4a6a57880edcbc` received an independent **`SCOPED ACCEPT`** in review commit `5a4b9381d4863d10a1d46b249c0be83e17cee18f` (`docs/reviews/CONVERSATION-INTEGRATION.md`, reviewer `platform-conv-review`, covering `453` Worker tests, `635` host tests, `30` mock UI checks, `89` real PNG screenshots, `9` public CLI pairing groups, and `10` native groups; head independently verified all `199` outputs + `88` table rows and reconciled reviewer `4881` journal `14952050`, cursors `3,4`, `1` delivery, `1` done reply, `1` audit, `0` pending).
  - *Recorded Metadata Errata on `5a4b9381d4863d10a1d46b249c0be83e17cee18f`*: The report header in `5a4b9381d4863d10a1d46b249c0be83e17cee18f` listed direct parent `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24` / `GPT-5.4` rather than the actual proposal parent `f9f6fb21495763a025610878a7f0606b6a0c8d71` / `GPT-6`; the underlying code and artifact verification on `cfb4dc26985cbe39a1ae770ade4a6a57880edcbc` are unaffected.
  - *Main Integration Status*: `platform` is preparing `main` integration of the accepted CFB source in a physically disjoint owned worktree while preserving the shared Hub checkout and active owner leases.

### 2.2 Individual Windows Source Review (`3d0dfadbed35812aeaacbb98f304acfecf711e91`)

- Commit `3d0dfadbed35812aeaacbb98f304acfecf711e91` (`docs/reviews/WINDOWS-CONVERSATION.md`, reviewer `platform-win-review/claude-opus-4-7` on direct parent `2de0a6c1846b110227cd1d15e99a7af6999158f5`) issued a `SCOPED ACCEPT` on the standalone Windows patch (`bf2f3df7070c2c9145ca86dd4ada7e5ff6399c6e478284807e0d1009b5d61500` over `0896ebe157cc732342b17fb40a6cf6e096ef93a2`) with `205` unit tests (`1` actual-Windows skip, `71` subtests), `16+3` stdlib/import tests, `58` compiled Python files, and `7` reviewer probes (`13` subtests), and head independently reconstructed its `142` inputs, `146` outputs, and `10` table rows.
- *Limitations of `3d0dfadbed35812aeaacbb98f304acfecf711e91` Addressed Here*:
  1. That review tested the patch over standalone P3 (`0896ebe157cc732342b17fb40a6cf6e096ef93a2`), not composed with `cfb4dc26985cbe39a1ae770ade4a6a57880edcbc` (`f9f6fb21495763a025610878a7f0606b6a0c8d71` public CLI auto-machine persistence + full Worker/native stack).
  2. That reviewer removed its reconstructed candidate and probe scripts before handoff (retaining only logs/manifests); in this review, `.local/review/candidate`, `.local/review/test_reviewer_probes.py`, `.local/review/public-native-review.mjs`, `.local/review/driver-review.diff`, all manifests, and all diagnostic and passing logs are retained intact for head inspection.
  3. That review cited no primary Microsoft/Python SDK documentation URLs; Section 5 below verifies the `win32_fs.py` ctypes structs, flags, calling conventions, and `msvcrt` semantics directly against official Microsoft Learn and Python 3.12 documentation with exact links.

---

## 3. Exact Git-Object Reconstruction & Digest Verification

All reconstruction and hash checks were executed by `.local/review/reconstruct_and_verify.py` (summary saved in `.local/review/reconstruction-summary.json`, full 199-file input manifest in `.local/review/cfb-input-manifest.json`, and full 203-file output manifest in `.local/review/composed-output-manifest.json`).

### 3.1 Commit & Artifact Digests

| Artifact / Tree | Commit / Path | SHA-256 / Value | Status |
| :--- | :--- | :--- | :--- |
| Reviewed commit (`HEAD`) | `d79fab0175fc57a0909da41d8f5d9f5ef4c0bba9` | Parent `981430f4793160e4580789889e13d5c29e0a824f` -> `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24` | Verified |
| Proposal document | `docs/proposals/WINDOWS-INTEGRATION.md` | `1b685be09932a00bea329e916cefbbbe30bf1e5e14ded6a90f9b33ee3ef25be4` | Verified |
| Committed Windows patch | `docs/proposals/WINDOWS-INTEGRATION.patch` | `bf2f3df7070c2c9145ca86dd4ada7e5ff6399c6e478284807e0d1009b5d61500` | Byte-identical to `2de0a6c1846b110227cd1d15e99a7af6999158f5:docs/proposals/WINDOWS-CONVERSATION.patch` |
| Frozen CFB patch | `cfb4dc26985cbe39a1ae770ade4a6a57880edcbc:docs/proposals/CONVERSATION-INTEGRATION.patch` | `e8c91a9c3f3a795360c85eec300d3df9187a361d57f9a9dfc9312ad60e673d10` | Verified (`0` adaptations) |
| Base `main` manifest (`131` files) | `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24` | `cf82fcb509d371f36e67bd314d9b8f4d699f7889d093e1c2b6ef8a0f00394c5c` | All `131` non-proposal tracked worktree files match |
| CFB input manifest (`199` files) | `main` + `CONVERSATION-INTEGRATION.patch` | `17aab235373ff0f98cce9ff361b74333d9d3bd23bf45790d471865f3ea02223a` | Verified |
| Composed output manifest (`203` files) | CFB + `WINDOWS-INTEGRATION.patch` | `5fb8eeace9f7b936e1b9043902cea3783cd0aa72decabedb51f96bef298e8804` | Verified pre-test and post-test (`193` non-Windows CFB files unchanged) |

**Manifest Algorithm Verified**: Both `17aab235373ff0f98cce9ff361b74333d9d3bd23bf45790d471865f3ea02223a` (`199` files) and `5fb8eeace9f7b936e1b9043902cea3783cd0aa72decabedb51f96bef298e8804` (`203` files) are the SHA-256 of `json.dumps([[rel_posix_path, file_sha256], ...], separators=(",", ":")).encode("utf-8")` over sorted regular non-symlink files excluding top-level `{".local", "node_modules", "dist", ".wrangler", ".git"}` and any path containing `__pycache__`.

### 3.2 Ten-Path Input/Output Hash Table Verification

| Path | CFB Input SHA-256 (`199`-file tree) | Composed Output SHA-256 (`203`-file tree) | Verification |
| :--- | :--- | :--- | :--- |
| `host/hub_agent/bridge.py` | `3fb3bfaa8a3789da8126be9d478bf63f1387003c320d4261da57be119c8fc8f8` | `692f343322beeb10e9a973e9e4c53ab906db7266586ac5b4b2cb6b46080a943f` | Exact match |
| `host/hub_agent/config.py` | `159bd9c1093585da818aa511efdd7a63e675ef2f90e319001610622fb6c4c635` | `35f39cb2c260eb6443d3dbba2caad204f9202e6958e3ebfa5ed91cb334077875` | Exact match |
| `host/hub_agent/conversation_bridge.py` | `f6ebdb9eb9458931b319c16c17df515bd9d2df62ad4f5414474e55ff15b29387` | `cc9a9f0c205d8814a562db74bd7c3b833339d243b060d81d70db8e97f45f6bd7` | Exact match |
| `host/hub_agent/native_bridge.py` | `7810ccab01c78eb24041b40d1e9a4c93db5fb12efbe881da237aa10a08d0df63` | `d19242798672940bd96ef87a59268a4d5c9a71c5ba9b45bf8061a6b731cfdb5a` | Exact match |
| `host/hub_agent/private_fs.py` | `NEW` (verified absent) | `dfce9a087026e97d6ca0977e0037e938a3918b35e7b2bf6d3448a5b942b870a2` | Exact match |
| `host/hub_agent/win32_fs.py` | `NEW` (verified absent) | `0565ac941b4bc493c9c907ec595559994a2b789219a0faefcd8f40bd85b4ca26` | Exact match |
| `host/tests/test_bridge.py` | `fd6c089983a19b2805e9c84ef59be44da3fa730a2b82a54eb1bbca2da6ba69ff` | `677f22e9ed7c9c6b4e935a4469bc202863a8aaec3e0f6f8a00c367c0aa0a8da4` | Exact match |
| `host/tests/test_config.py` | `83dbcb1425163222b2382b6f6a34cb8e692c8ddbd1be1da3d8abfdca98c845cf` | `13493a3843de3c4b39da8544f48914373e25d27b8d3866c55b0b807a0b64e97c` | Exact match |
| `host/tests/test_private_fs.py` | `NEW` (verified absent) | `efaa67e04d946f648e48cf65694fc6a02cfbc5d523ce79619105375aa041c231` | Exact match |
| `host/tests/test_windows_imports.py` | `NEW` (verified absent) | `4234abef351b846b8db635c4dd1161f6fd47a278202c6ec85be39ef6b8f392c3` | Exact match |

---

## 4. Code & Composition Audit (`private_fs.py` + External Module Composition)

### 4.1 POSIX Private Storage Backend (`host/hub_agent/private_fs.py`)

- **Parent-Pinned Descriptor Operations (`_posix_parent_dir_fd`, `validate_private_parent`)**:
  - Rejects relative paths (`ValueError`), refuses symlinks on the target or parent (`O_NOFOLLOW`), and opens the parent directory with `O_RDONLY | O_CLOEXEC | O_DIRECTORY | O_NOFOLLOW`.
  - Requires `stat.S_ISDIR(st.st_mode)`, owner UID equality (`st.st_uid == os.geteuid()`), and zero group/world permission bits (`st.st_mode & 0o077 == 0`), raising `PermissionError` otherwise.
- **Open Regular File Validation (`open_no_follow`, `validate_open_regular_fd`)**:
  - Opens relative to `dir_fd` with `O_NOFOLLOW | O_CLOEXEC` and mode `0600`.
  - Validates `fstat(fd)` is a regular file (`S_ISREG`), single hardlink (`st.st_nlink == 1`), owner UID match, and zero executable/group/world bits (`st.st_mode & 0o177 == 0`).
  - Verifies `(st.st_dev, st.st_ino)` matches `os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)` so an unlinked or replaced inode cannot pass validation.
- **Randomized Same-Parent Atomic Write (`atomic_write_bytes`)**:
  - Creates `.json.<32lowercasehex>.tmp` (`f".{path.name}.{secrets.token_hex(16)}.tmp"`) inside `dir_fd` using `O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC` with mode `0600` and `fchmod(fd, 0o600)`.
  - Writes the full payload, calls `os.fsync(fd)`, closes `fd`, atomically replaces via `os.replace(tmp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)`, and flushes directory metadata via `os.fsync(dir_fd)`.
  - Cleans up any leftover `tmp_name` in `finally` (`os.unlink(tmp_name, dir_fd=dir_fd)`) before closing `dir_fd`.
- **Advisory Exclusive Lock (`exclusive_lock`)**:
  - Opens/validates the `.lock` file in `dir_fd`, acquires `fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)`, and re-verifies `fstat(fd)` matches `stat(path.name, dir_fd=dir_fd, follow_symlinks=False)` after acquiring the lock to prevent lock-file replacement races.

### 4.2 External Module Composition Across `config.py`, `bridge.py`, `conversation_bridge.py`, and `native_bridge.py`

1. **`host/hub_agent/config.py`**:
   - Imports `atomic_write_bytes`, `open_no_follow`, `validate_private_parent` from `hub_agent.private_fs`.
   - Adds Windows root resolution (`LOCALAPPDATA`, `APPDATA`, `USERPROFILE`) when `os.name == "nt"` and `XDG_CONFIG_HOME` / `HUB_CONFIG_FILE` are unset, while preserving POSIX XDG defaults (`~/.config/agent-hub/config.env`).
   - Preserves `save_config_values()` / `_persist_authoritative_machine_id()` from CFB `f9f6fb21495763a025610878a7f0606b6a0c8d71` while upgrading file creation and replacement to `validate_private_parent` + `open_no_follow` + `atomic_write_bytes`.
2. **`host/hub_agent/bridge.py`**:
   - Replaces Unix-only `import fcntl` / `import pwd` with `from hub_agent.private_fs import PrivateJsonFile` and portable `_current_username()`.
   - All CFB public CLI conversation commands (`conversations list/create/get/send/read/bridge`), device login (`hub login`), automatic authoritative `machine_id` persistence (`_persist_authoritative_machine_id`), `None` `machine_id` tolerance prior to pairing, `HUB_MACHINE_ID` conflict refusal (`Configured HUB_MACHINE_ID ... does not match Hub token machine_id ...`), and ordinary-agent privilege guards remain intact. No regression to old `1149` manual-pairing semantics exists.
3. **`host/hub_agent/conversation_bridge.py`**:
   - Replaces inline POSIX `fcntl.flock` and fixed `.json.tmp` replacement with `PrivateJsonFile`, `atomic_write_bytes`, `exclusive_lock`, `open_no_follow`, and `validate_private_parent`.
   - Preserves `JOURNAL_VERSION = 1`, `MAX_JOURNAL_BYTES = 8 * 1024 * 1024`, strict 7-tuple identity (`[origin, actor, project, workspace, machine, session_id, tag]`), pending delivery/reply `HOLD` before any cursor or `read_seq` advancement, P3 explicit gap recovery (`--enable-native --acknowledge-gap`, empty retained range proof via ascending `after_seq`, atomic `gaps` + `cursors` write without advancing server `read_seq`, equal-target replay refusal), and `member(cid)` verification before every native/Hub effect.
4. **`host/hub_agent/native_bridge.py`**:
   - Adds portable `_state_dir_candidates()` (`LOCALAPPDATA/aplexer`, `APPDATA/aplexer` on Windows; `XDG_STATE_HOME/aplexer`, `~/.local/state/aplexer` on POSIX) and `_binary_names()` (`a.exe`, `aplexer.exe` on Windows; `a`, `aplexer` on POSIX) while keeping all native session discovery, workspace/session/tag verification, and read-only qualification predicates unchanged.

---

## 5. Primary Microsoft Win32 & Python SDK Verification (`host/hub_agent/win32_fs.py`)

Because this review runs on Linux (`x86_64`), physical execution of Windows DLLs (`kernel32.dll`, `advapi32.dll`, `ucrtbase.dll`) and real NTFS security descriptors / reparse points is not possible on this host and remains gated on the Windows owner handoff. However, every Win32 ABI declaration, structure layout, constant value, and `msvcrt` call in `host/hub_agent/win32_fs.py` was audited directly against primary Microsoft Learn and Python 3.12 documentation:

1. **`BY_HANDLE_FILE_INFORMATION` (`FileInfo`) & `GetFileInformationByHandle`**:
   - Reference: [BY_HANDLE_FILE_INFORMATION structure (fileapi.h)](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/ns-fileapi-by_handle_file_information) and [GetFileInformationByHandle function (fileapi.h)](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfileinformationbyhandle).
   - Verified layout: `dwFileAttributes` (`DWORD`), `ftCreationTime` (`FILETIME`, 2x `DWORD`), `ftLastAccessTime` (`FILETIME`), `ftLastWriteTime` (`FILETIME`), `dwVolumeSerialNumber` (`DWORD`), `nFileSizeHigh` (`DWORD`), `nFileSizeLow` (`DWORD`), `nNumberOfLinks` (`DWORD`), `nFileIndexHigh` (`DWORD`), `nFileIndexLow` (`DWORD`) = `13 * 4 = 52` bytes (`sizeof(FileInfo) == 52` verified in Python).
   - Used in `validate_open_handle()` to reject `FILE_ATTRIBUTE_DIRECTORY` (`0x10`) for regular files, reject `FILE_ATTRIBUTE_REPARSE_POINT` (`0x400`), enforce `nNumberOfLinks == 1` on regular files, and compare `(dwVolumeSerialNumber, (nFileIndexHigh << 32) | nFileIndexLow)` across handle and path opens.
2. **`CreateFileW` Flags, Share Mode, and Reparse/Directory Semantics**:
   - Reference: [CreateFileW function (fileapi.h)](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew) and [File Security and Access Rights](https://learn.microsoft.com/en-us/windows/win32/fileio/file-security-and-access-rights).
   - Verified constants: `GENERIC_READ = 0x80000000`, `GENERIC_WRITE = 0x40000000`, `READ_CONTROL = 0x00020000`, `WRITE_DAC = 0x00040000`, `FILE_SHARE_READ | FILE_SHARE_WRITE = 0x00000003` (excludes `FILE_SHARE_DELETE = 0x00000004` so an open validated file/lock handle cannot be unlinked or renamed out from under the holder), `CREATE_NEW = 1`, `OPEN_EXISTING = 3`, `OPEN_ALWAYS = 4`, `FILE_ATTRIBUTE_NORMAL = 0x00000080`, `FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000`, `FILE_FLAG_BACKUP_SEMANTICS = 0x02000000`.
   - Using `FILE_FLAG_OPEN_REPARSE_POINT` ensures `CreateFileW` opens the reparse point itself rather than following a symlink/junction/mount point, allowing `validate_open_handle` (`dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT`) to deterministically refuse it.
3. **`GetFinalPathNameByHandleW` Canonical Path & Ancestor Pinning**:
   - Reference: [GetFinalPathNameByHandleW function (fileapi.h)](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfinalpathnamebyhandlew).
   - Called with `dwFlags = 0` (`FILE_NAME_NORMALIZED | VOLUME_NAME_DOS`). Rejects UNC paths, Alternate Data Streams (`:` after drive letter), and device-namespace escapes; walks every ancestor directory above the drive root with `FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT` to refuse any intermediate symlink or junction.
4. **Owner SID & DACL Verification (`GetSecurityInfo`, `GetSecurityDescriptorControl`, `ConvertStringSecurityDescriptorToSecurityDescriptorW`, `ACL`, `ACCESS_ALLOWED_ACE`)**:
   - References:
     - [GetSecurityInfo function (aclapi.h)](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-getsecurityinfo)
     - [GetSecurityDescriptorControl function (securitybaseapi.h)](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-getsecuritydescriptorcontrol)
     - [ConvertStringSecurityDescriptorToSecurityDescriptorW function (sddl.h)](https://learn.microsoft.com/en-us/windows/win32/api/sddl/nf-sddl-convertstringsecuritydescriptortosecuritydescriptorw)
     - [ACL structure (winnt.h)](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-acl)
     - [ACCESS_ALLOWED_ACE structure (winnt.h)](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-access_allowed_ace)
   - Verified constants and layouts: `SE_FILE_OBJECT = 1`, `OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION = 0x00000005`, `SE_DACL_PROTECTED = 0x1000`, `SDDL_REVISION_1 = 1`, `ACL` (`8` bytes: `AclRevision`, `Sbz1`, `AclSize`, `AceCount`, `Sbz2`), `ACCESS_ALLOWED_ACE` (`AceType == 0`, `AceFlags`, `AceSize`, `Mask`, `SidStart` at byte offset `8`, verified `ACE.SidStart.offset == 8`).
   - `_apply_owner_only_dacl` builds `D:P(A;OICI;FA;;;{owner_sid})` for directories and `D:P(A;;FA;;;{owner_sid})` for files and applies the protected DACL via `SetSecurityInfo(..., DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION, ...)`.
   - `_assert_handle_owner_only_acl` verifies the token user SID matches the object owner SID (`EqualSid`), requires `SE_DACL_PROTECTED` (`control & 0x1000 != 0`) and `AceCount > 0`, and verifies every ACE is `ACCESS_ALLOWED_ACE_TYPE` (`0`) whose SID equals the current token owner SID (rejecting `Everyone`, `Users`, `Authenticated Users`, `Administrators`, `SYSTEM`, or any other principal).
5. **Atomic Replacement (`MoveFileExW`)**:
   - Reference: [MoveFileExW function (winbase.h)](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw).
   - Verified flags: `MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH = 0x00000001 | 0x00000008 = 0x00000009` after `FlushFileBuffers(h)` on the `.json.<32lowercasehex>.tmp` sibling handle.
6. **CRT File-Descriptor Bridge & Region Locking (`msvcrt.open_osfhandle`, `msvcrt.locking`)**:
   - Reference: [msvcrt — Useful routines from the MS VC++ runtime (Python 3.12 documentation)](https://docs.python.org/3/library/msvcrt.html).
   - Verified constants and ownership semantics: `msvcrt.LK_NBLCK = 2`, `msvcrt.LK_UNLCK = 0`. Once `msvcrt.open_osfhandle(handle, flags)` succeeds, the C runtime file descriptor owns the underlying OS `HANDLE` and `os.close(fd)` closes it; `win32_fs.py` correctly avoids calling `CloseHandle(handle)` after `open_osfhandle` succeeds, and wraps `os.close(fd)` with `try/except OSError` if `open_osfhandle` fails before transfer.

---

## 6. Verification of Author's Retained Evidence & Diagnostics

Before running independent checks, the author's retained evidence in `.local/windows/` (`4a0b74cb-7b0e-4092-aa8a-20af4da7f3ae`) was inspected read-only:

- **Author's First-Run Diagnostic vs. Corrected Union**:
  - Author retained `.local/windows/host-union-first-fail.log` (`649` PASS, `4` mock HTTP errors in `test_bridge.py`, `1` Windows skip). Root cause verified: the author's initial runner exported a global `HUB_CONFIG_FILE` override (causing child CLI processes in `test_bridge.py` to ignore their per-test `XDG_CONFIG_HOME` fixture configs) and ran under default `umask 002` (creating temporary parent directories with mode `0775`, which `validate_private_parent` rightly rejected).
  - Removing the runner-level `HUB_CONFIG_FILE` override and running child processes under `umask 077` (with zero product or test code edits) produced `.local/windows/host-focused-config-bridge.log` (`8/8` PASS) and `.local/windows/host-union.log` (`654` tests = `653` PASS + `1` Windows skip, `749` subtests in `.local/windows/host-subtests.txt`).
- **Author's Native Driver & Reconciled Journal**:
  - Verified `.local/windows/public-native.mjs` SHA-256 = `cefbe66cecc58749e8465e080a01ac3ff19d7bf62e979f1fbbacaf6d84204d92` and `.local/windows/driver.diff` SHA-256 = `3fa2da7379d11c3dff9dc56e8de5ed12375236cc7281a5d8b721f21c28f345eb` against frozen `test/conversation-platform.mjs`: changes are strictly limited to the candidate root path, own workspace/identity (`platform-win-merge`, `4a0b74cb-7b0e-4092-aa8a-20af4da7f3ae`), and the read-only `mailBeforeGap` / `mailAfterGap` zero-envelope assertion.
  - Verified author's retained journal `.local/conversation-bridge-c3525bd5b4aa42dc49c57111.json` SHA-256 = `db6ff994ad845fa28fef62ac3cf604e4cdf8bc7af3e49faf3b7a7cfd34abc128` (`0600`, lock `0600`, parent `.local` `0700`, cursors `3` and `4`, `1` delivery, `1` done reply, `1` gap audit, `0` pending). Author's journal and identity were left strictly untouched.

---

## 7. Independent Reviewer Verification, Commands & Unique Counts

All independent checks were run serially on CPU core `0` (`taskset -c 0`) under `umask 077` with `UV_THREADPOOL_SIZE=1`, `NODE_OPTIONS=--v8-pool-size=1`, `GOMAXPROCS=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONNOUSERSITE=1`, and `TMPDIR` inside `.local/tmp` via `.local/review/run_independent_checks.py`.

### 7.1 Summary of Independent Verification Stages

| Stage | Command / Harness | Unique Count / Outcome | Retained Evidence Path |
| :--- | :--- | :--- | :--- |
| **0. Git-Object Reconstruction & Manifest Check** | `python3 .local/review/reconstruct_and_verify.py` | `131` base files (`cf82fcb5...`), `199` CFB files (`17aab235...`), `203` composed files (`5fb8eeac...`), `10/10` table rows, `193/193` non-Windows CFB files unchanged | `.local/review/reconstruction-summary.json`, `.local/review/cfb-input-manifest.json`, `.local/review/composed-output-manifest.json` |
| **1. Host Python Bytecode Compilation** | `py_compile.compile` across `candidate/host/**/*.py` | **`58/58` files compiled** (`0` errors) | `.local/review/compile-tracked.json` |
| **2. Composed Host `unittest` Union** | `unittest.defaultTestLoader.discover("candidate/host/tests")` with `SubtestCountingResult` | **`654` tests** (`653` PASS + `1` actual-Windows SKIP), **`749` passing subtests** (`0` failures, `0` errors) | `.local/review/host-union.log`, `.local/review/host-union-counts.json` |
| **3. Reviewer Adversarial POSIX Probes** | `python3 .local/review/test_reviewer_probes.py -v` | **`6/6` probe methods PASS** (**`9` subtests** PASS) | `.local/review/test_reviewer_probes.py`, `.local/review/reviewer-probes.log` |
| **4. TypeScript Typecheck** | `npm run typecheck` (`tsc --noEmit`) | **PASS** (`0` errors) | `.local/review/typecheck.log` |
| **5. Composed Worker Vitest Suite** | `npm test -- --config test/registry-f7.config.ts ...` (`7` test files) | **`92/92` tests PASS** across `7` files (`33+12+15+14+11+5+2`) | `.local/review/worker-composed.log` |
| **6. Official Dakit Lock & Static UI Checks** | `node test/existing-ui.checks.mjs` & `node test/conversations-ui.checks.mjs` | **`21` locked dakit files** + **`2/2` UI check scripts PASS** | `.local/review/kit-existing-ui.log`, `.local/review/kit-conversations-ui.log` |
| **7. Public Machine-Pairing Browser/CLI/Refresh** | `node test/machine-pairing.mjs` (with read-only native qualify on own UUID `47b53dcb-79e7-431b-b2a7-5bb02e2cdc37`) | **`9/9` groups PASS** (`0` journal/inbox writes) | `.local/review/public-pairing.log` |
| **8. Local Bundle Dry-Run Validation** | `npm run build` (`wrangler deploy --dry-run --outdir dist`) | **PASS** (`552.38 KiB` / `105.98 KiB` gzip, `HUB` DO binding verified) | `.local/review/bundle.log` |
| **9. Own-Session Native POSIX + Public CLI + P3** | `node .local/review/public-native-review.mjs --native --project=cint-local-winmergereview` | **`10/10` groups PASS** (`8` preflight repeats + `2` unique native groups; `0` mail in gap stage, `1` own envelope, `1` own reply, `0` pending) | `.local/review/native-posix.log`, `.local/review/native-reconciliation.json` |

*Note on UI Screenshot Scope*: Because all `193` non-Windows CFB files (including all Worker TypeScript sources and all `public/conversations/` and `public/` assets) are byte-for-byte identical to `cfb4dc26985cbe39a1ae770ade4a6a57880edcbc` (`SCOPED ACCEPT` in `5a4b9381d4863d10a1d46b249c0be83e17cee18f`), the `89` visual regression screenshots from CFB review were not re-rendered; static UI lock checks (`Stage 6`) and real Chromium Playwright flows in `Stage 7` and `Stage 9` verified the live browser/CLI/native integration path against the new `private_fs` backend.

### 7.2 Detail of Independent Reviewer Adversarial Probes (`.local/review/test_reviewer_probes.py`)

1. **`test_01_multiprocess_lock_contention_and_sigkill_release`**:
   - Spawns a separate child process (`multiprocessing.get_context("spawn")`) holding `ConversationJournal.lock()` (`fcntl.flock(LOCK_EX | LOCK_NB)`).
   - Verifies a competing parent `ConversationJournal.lock()` fails closed with `BridgeCapabilityGap` (`"Conversation bridge is already running for this journal"`).
   - Sends `SIGKILL` (`-9`) to the lock-holding child process, waits for process death, and verifies the parent immediately acquires `ConversationJournal.lock()` cleanly (proving OS-level descriptor lock release on abrupt process termination without stale lock-file manual cleanup).
2. **`test_02_atomic_write_random_hex_tmp_and_fsync_failure_rollback`** (`4` subtests):
   - Intercepts `os.open` during `atomic_write_bytes` and verifies every staging filename matches `^\.conversation-bridge-probe\.json\.[0-9a-f]{32}\.tmp$` (never the old fixed `.json.tmp`).
   - Injects `OSError(errno.EIO, "Simulated disk fsync failure")` into `os.fsync` during a journal update and verifies `atomic_write_bytes` unlinks the `.tmp` file in `finally`, leaves `0` `.tmp` leftovers in `.local`, and preserves the prior committed journal bytes intact.
3. **`test_03_symlink_hardlink_and_parent_permission_refusal`** (`3` subtests):
   - Verifies `chmod 0755` on parent directory fails closed (`BridgeCapabilityGap` wrapping `PermissionError`).
   - Verifies a symlinked journal path fails closed (`BridgeCapabilityGap` wrapping `OSError`/`ELOOP`).
   - Verifies a hardlinked journal file (`st_nlink == 2`) fails closed (`BridgeCapabilityGap` wrapping `PermissionError: Refusing hardlinked private file`).
4. **`test_04_journal_8mib_limit_and_7tuple_identity_mismatch`** (`2` subtests):
   - Verifies a journal file of `MAX_JOURNAL_BYTES + 1` (`8,388,609` bytes) is refused before JSON parsing (`BridgeCapabilityGap: exceeds maximum size`).
   - Verifies mutating each of the `7` identity slots (`origin`, `actor`, `project`, `workspace`, `machine`, `session_id`, `tag`) individually raises `BridgeCapabilityGap("Conversation bridge journal identity does not match current binding")`.
5. **`test_05_pending_delivery_and_reply_hold_blocks_advance`**:
   - Verifies an unresolved delivery record (`attempted=False, native_id=None`) causes `run_conversation_bridge` to return `status="HOLD"` (`"Pending native delivery 01C:1 requires reconciliation"`) with `0` `mark_read` calls and cursor unchanged.
   - Verifies an unresolved reply record (`done=False`) causes `run_conversation_bridge` to return `status="HOLD"` without re-delivering or advancing cursor/`read_seq`.
6. **`test_06_p3_gap_retained_refusal_and_acknowledge_atomic_audit_without_read_advance`**:
   - With local cursor `1`, server `read_seq=3`, `last_seq=3`, verifies default `run_conversation_bridge` returns `status="HOLD"` (`"History is not contiguous/canonical; HOLD before native delivery/read"`).
   - Verifies `acknowledge_gap="01CGAP:2"` is refused when sequence `2` is still present in retained history (`"Retained message 2 exists inside gap range 2..2"`).
   - When sequence `2` is genuinely missing (only sequence `3` retained), verifies `acknowledge_gap="01CGAP:2"` atomically records the `gaps` audit entry (`missing_from_seq=2, missing_through_seq=2, old_cursor=1, new_cursor=2, server_read_seq=3`) and advances local cursor to `2` while issuing **`0` `mark_read` calls** to the server, and refuses a second replay of `acknowledge_gap="01CGAP:2"`.

### 7.3 Retained Reviewer Diagnostic Logs (Preserved in `.local/review/`)

In accordance with review retention rules, all four intermediate diagnostic logs encountered while setting up the isolated reviewer harnesses were preserved in `.local/review/`:

1. **`.local/review/host-union-fake-home.log` & `.local/review/host-union-counts-fake-home.json`**:
   - Initial host union run where the reviewer harness overrode `HOME` to an empty directory (`xdg-isolated/home`). Because `test_native_bridge.py` discovers aplexer sessions under `Path.home() / ".local/state/aplexer"`, overriding `HOME` caused `3` native-discovery unit tests to skip (`650` PASS + `4` SKIP). Re-running with genuine `HOME` while isolating `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, and `XDG_RUNTIME_DIR` produced the exact `653` PASS + `1` actual-Windows SKIP (`host-union.log`).
2. **`.local/review/reviewer-probes-module-invocation.log`**:
   - Initial invocation of `python3 -m unittest /abs/path/.local/review/test_reviewer_probes.py`, where Python's `unittest` CLI interpreted `.local/review/...` as a dotted module path because of the leading dot in `.local`. Switched to direct script execution (`python3 .local/review/test_reviewer_probes.py -v`).
3. **`.local/review/reviewer-probes-blockingio.log`**:
   - Initial probe assertion expecting raw `BlockingIOError` from `ConversationJournal.lock()`, whereas `ConversationJournal.lock()` catches `(BlockingIOError, OSError)` from `exclusive_lock()` and raises `BridgeCapabilityGap("Conversation bridge is already running for this journal")`. Updated the probe expectation to assert `BridgeCapabilityGap` with `"already running"`.
4. **`.local/review/native-posix-esm-resolution.log`**:
   - Initial invocation of `node .local/review/public-native-review.mjs --native --project=cint-local-winmergereview` failed during Node ESM static module linking (`ERR_MODULE_NOT_FOUND: Cannot find package 'miniflare'`) before line 1 of the script executed, because `node_modules` was symlinked inside `.local/review/candidate/node_modules` rather than `.local/review/node_modules`. Verified `0` journal/lock/path files and `0` `hub-conversation` native messages were created, reported the causal diagnosis to `platform` (`01a147fd-0615-7b12-bf24-23ed3b12da87`), created the `.local/review/node_modules` symlink to the existing shared `node_modules`, and waited for explicit head ACK (`01a147ff-9d9c-7762-b99b-2b7d8296269d`) before executing Stage 9.

---

## 8. Genuine Own-Session Native POSIX + Public CLI + P3 Reconciliation

Stage 9 was executed once under explicit head authorization (`01a147f1-40fc-7c93-83a5-9d3027b78ca5` and `01a147ff-9d9c-7762-b99b-2b7d8296269d`) using `.local/review/public-native-review.mjs` (SHA-256 `f81748f932276dbe95975245a0029e0cb9d822b7d8f5c9d69531312c5cc49825`; unified diff against frozen `test/conversation-platform.mjs` saved at `.local/review/driver-review.diff`, SHA-256 `c40b107ad3b9a91eaf2a23e11876c80636c66359bcc951bb52d915db826460f7`).

### 8.1 Reconciled Journal & Native Effect Summary (`.local/review/native-reconciliation.json`)

- **Reviewer Session Identity**:
  - `session_id`: `47b53dcb-79e7-431b-b2a7-5bb02e2cdc37`
  - `tag`: `platform-win-merge-review`
  - `engine`: `antigravity`
  - `project`: `cint-local-winmergereview`
  - `actor`: `cint-local-winmergereview:platform-win-merge-review`
  - `machine`: `cint-machine` (persisted automatically via public CLI `hub login` + signed local human/current CSRF approval)
- **Journal & Lock Files**:
  - Journal path: `.local/conversation-bridge-a86c4b4f1f620ef37af5b422.json`
  - Journal SHA-256: `8360ff91bce42ec3732efc35b9701ae6e6fcf9cd9eebf99b92d80a3b64582c87` (`2,173` bytes, mode `0600`)
  - Lock path: `.local/conversation-bridge-a86c4b4f1f620ef37af5b422.json.lock` (`0` bytes, mode `0600`)
  - Parent directory: `.local` (mode `0700`)
  - Staging `.tmp` leftovers in `.local`: `[]` (`0` files)
- **Stage 9a — Gap-First Zero-Mail Verification (`conversation_id = 01M5400Y3M8VEJXJKPA1B68PX6`)**:
  - Verified `mailBeforeGap == mailAfterGap` (`zeroNativeEnvelopes: true`): zero native `hub-conversation` messages were emitted during the entire P3 retention-gap stage.
  - With local cursor `1` and server `read_seq = 3`, `last_seq = 3` (sequence `2` purged, sequence `3` retained), default bridge execution returned `HOLD` (`"History is not contiguous/canonical; HOLD before native delivery/read"`).
  - Attempting `--acknowledge-gap 01M5400Y3M8VEJXJKPA1B68PX6:3` was refused because sequence `3` is still retained on the server.
  - Running `--enable-native --acknowledge-gap 01M5400Y3M8VEJXJKPA1B68PX6:2` verified range `2..2` is absent, atomically wrote the `gaps` audit record (`old_cursor: 1`, `new_cursor: 2`, `missing_from_seq: 2`, `missing_through_seq: 2`, `server_read_seq: 3`, `server_last_seq: 3`, `reason: "operator_acknowledged_missing_history"`), kept server `read_seq` unchanged at `3`, refused an equal-target replay of `--acknowledge-gap 01M5400Y3M8VEJXJKPA1B68PX6:2`, and caught up to cursor `3` on reconnect without native delivery effects.
- **Stage 9b — Single Own Human Envelope, Single Own Reply & Restart Deduplication (`conversation_id = 01M5400K6Q6NXXHN61TNA90R4P`)**:
  - **Delivered Native Envelope (`1` total)**:
    - Native message ID: `01a14800-8d7a-7fc0-849a-b1bd1d488509` (`kind: "hub-conversation"`, `from` and `to`: `platform-win-merge-review` / `47b53dcb-79e7-431b-b2a7-5bb02e2cdc37`)
    - Hub message ID: `01M5400KEYP6XGYTY5FN5S3VZC` (`seq: 1`, `sender_actor: "human:cint-human"`, `body: "browser nonce f2d8299be600e84e"`)
    - Bridge provenance token (`data.hub_conversation_bridge`): `5df7a58a4b6f3a9b1e2543fa0d9c63c29b55f9719e31fcf0391bc67a05df2d72`
    - Journal delivery state: `deliveries["01M5400K6Q6NXXHN61TNA90R4P:1"].attempted == true`, `native_id == "01a14800-8d7a-7fc0-849a-b1bd1d488509"`
  - **Relayed Native Reply (`1` total)**:
    - Native reply ID: `01a14800-901f-7a01-868b-9f358abff1d0` (`kind: "reply"`, `reply_to: "01a14800-8d7a-7fc0-849a-b1bd1d488509"`, `body: "native reply f2d8299be600e84e"`)
    - Journal reply state: `replies["01a14800-901f-7a01-868b-9f358abff1d0"].done == true`, `idempotency_key: "native:77a458f4cc3b1b50a77b475fc08bba8c517173b57f9b5781c9aff027827a125a"`, `reply_to: "01M5400KEYP6XGYTY5FN5S3VZC"`
  - **Final Cursors & Pending Predicate**:
    - `cursors`: `{"01M5400K6Q6NXXHN61TNA90R4P": 4, "01M5400Y3M8VEJXJKPA1B68PX6": 3}`
    - `deliveries`: `1`, `replies`: `1`, `gaps`: `1`, **`pending_unresolved`: `0`**
    - Post-run `203`-file candidate manifest SHA-256: `5fb8eeace9f7b936e1b9043902cea3783cd0aa72decabedb51f96bef298e8804` (unchanged).

---

## 9. Resource & Budget Accounting (`.local/review/resource-summary.json`)

| Metric | Measured Value | Budget / Gate | Status |
| :--- | :--- | :--- | :--- |
| Reviewer peak allocated scratch (`.local/review` + `.local/tmp` + own journal/lock) | **`7,163,904` bytes** (`6.83 MiB`) | `<= 31,457,280` bytes (`30.00 MiB`) | **PASS** |
| Reviewer peak apparent scratch | **`6,533,550` bytes** (`6.23 MiB`) | `<= 31,457,280` bytes (`30.00 MiB`) | **PASS** |
| Reviewer final allocated scratch (all evidence/candidate/logs retained) | **`5,931,008` bytes** (`5.66 MiB`) | `<= 31,457,280` bytes (`30.00 MiB`) | **PASS** |
| Reviewer final apparent scratch | **`5,367,005` bytes** (`5.12 MiB`) | `<= 31,457,280` bytes (`30.00 MiB`) | **PASS** |
| Workload cgroup `memory.peak` | **`722,096,128` bytes** (`688.64 MiB`) | `<= 1500 MiB` | **PASS** |
| Workload cgroup `pids.peak` | **`98` tasks** | `<= 256` tasks | **PASS** |
| Host `MemAvailable` | **`39,668,719,616` bytes** (`36.94 GiB`) | `>= 10 GiB` | **PASS** |
| Root filesystem free disk | **`77,947,842,560` bytes** (`72.59 GiB`) | `>= 50 GiB` | **PASS** |

---

## 10. Findings, Limitations & Owner Handoff Checklists

### 10.1 Code Findings

- **No blocking or non-blocking defects found in `d79fab0175fc57a0909da41d8f5d9f5ef4c0bba9`.**
  - The composition cleanly preserves all CFB `f9f6fb21495763a025610878a7f0606b6a0c8d71` public CLI, machine pairing, Worker/DO, and P3 semantics while adding parent-pinned `private_fs` / `win32_fs` private storage and portable path/import handling.

### 10.2 Open Limitations & Required Owner Handoffs

1. **POSIX Workspace `.local` `0700` Precondition (Base Migration Owner Handoff)**:
   - **Severity / Classification**: Operational migration precondition (intentional fail-closed security invariant in `private_fs.validate_private_parent`).
   - **Location**: `host/hub_agent/private_fs.py` (`validate_private_parent`, lines `63–72`) and `host/hub_agent/conversation_bridge.py` (`default_journal_path` / `ConversationJournal`).
   - **Behavior**: Existing workspaces where `<workspace>/.local` has group or world permission bits (`0775` or `0755`, e.g., created under `umask 002` or `022`) will fail closed with `PermissionError: Private directory <workspace>/.local must not grant group/world permissions` (surfaced as `BridgeCapabilityGap` by `ConversationJournal`).
   - **Bounded Owner Handoff & Rollback**:
     - Before enabling the conversation bridge in an existing POSIX workspace, the workspace owner must check `stat -c '%a %U' <workspace>/.local` and run a metadata-only `chmod 0700 <workspace>/.local` (never recursive `chmod -R`, and preserving any existing `.local/conversation-bridge-*.json` journals untouched).
     - Test runners must isolate `XDG_CONFIG_HOME` / `XDG_STATE_HOME` / `XDG_RUNTIME_DIR`, avoid setting a global `HUB_CONFIG_FILE` override over per-test fixtures, and run child processes under `umask 077`.
     - Rollback (if ever required) is a metadata-only permission restoration on `<workspace>/.local` without modifying or deleting journal files.
2. **Physical Windows Host & Win35 Runtime Verification (`M3b` / `M11` Gate)**:
   - **Severity / Classification**: Unproven physical host gate on Linux (`test_real_windows_reparse_refused_if_symlink_setup_supported` skipped on POSIX; Win32 DLL calls mocked in unit tests).
   - **Location**: `host/hub_agent/win32_fs.py` and `host/tests/test_private_fs.py` (`lines 198–236`).
   - **Bounded Windows Owner Handoff**:
     - On a physical Windows host (including `Win35` once re-enrolled under `M3b`/`M11`), run `python -m unittest discover -s host/tests -v` and confirm `test_real_windows_reparse_refused_if_symlink_setup_supported` runs and passes against real NTFS junctions/symlinks, owner-only SDDL DACLs (`D:P(A;OICI;FA;;;{owner_sid})`), `MoveFileExW` atomic replacement, and `msvcrt.locking` contention/crash release.
3. **Downstream Roadmap & Integration Separation**:
   - This `SCOPED ACCEPT` covers the composed source revision `d79fab0175fc57a0909da41d8f5d9f5ef4c0bba9`. Integration onto `main`, post-main `GATE-CONV-REVIEW`, and roadmap items `M4`, `M5`, `M7`, `M8a`, `M8b`, `M9`, `M10`, and `M11` remain active and governed by their respective owner leases and gates.
