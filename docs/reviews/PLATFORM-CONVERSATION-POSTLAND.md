# Factual audit of post-land Argon report `c195` (published source `16d66d69`)

- **Original immutable report commit**: `c1951e2c9f6a953cf876df9e5cdacb4cbfafbb39` (`docs(review): accept post-land conversation platform and Windows host source [skip ci]`; direct parent `16d66d69708268ab84089447534f0439e0bfd893`)
- **Published source pin**: `16d66d69708268ab84089447534f0439e0bfd893`
- **This auditor**: `platform-postland-factual-audit` / `grok-4.6` (session `e5aa637d-2b18-4f1f-b9ae-203ee6951eb3`)
- **Attributed Argon source review** (distinct identity; not replaced): `platform-postland-review` / `gemini-4-argon-high` (session `4d1c2347-80d5-4c23-acfb-996868462c01`) **SCOPED ACCEPT** of exact `16d66d69708268ab84089447534f0439e0bfd893` (`Review: approved sha=16d66d69708268ab84089447534f0439e0bfd893 reviewer=platform-postland-review/gemini-4-argon-high`). Probe/manifest execution remains theirs. This file is a publication pin/API/path audit only.

`c1951e2c9f6a953cf876df9e5cdacb4cbfafbb39` stays in Git unchanged. This file is a separate corrective audit on parent `16d66d69708268ab84089447534f0439e0bfd893`. It is not a replacement Argon gate, live deploy, or physical-delivery proof.

## Correction categories (original `c195` was not accurate in these)

1. **Nonexistent git object presented as M5 source**: `0a4a2fb95467fa96518068f68f15c0c93e5301e6` (native-session prefix, not a commit). Actual M5 candidate `117f24045ec402ccbd1791c2815f4f24e5bd1d7e`.
2. **Eleven paths listed in §2.2 as `5dc`→`7929` changes that are absent from the Git diff** (five of them do not exist at either pin).
3. **Invented or misnamed APIs/indexes/error codes** (behaviors often exist under different names).
4. **Wrong parallel-task commit strings** (`3e134565` missing; `7566b10d` is a session prefix).
5. **Windows patch size** `51,378` (actual `58,763` bytes; SHA-256 is correct).
6. **§2.2 grouping counts** inside the true 91-path diff (worker 10 vs 11, host `test_cli.py` vs `test_http_cli.py`, public 35 vs 30, `test/` 30 vs 34).
7. **Some cited line ranges** (`src/agent_registry.ts:318–440`, `src/ui-pages.ts:195–295`, `src/conversation-hub.ts:255–277`) exceed file length at `16`.
8. **M8 “launcher revision”** `6ee79d6ae13265135b89ca87d37d503de0099b76` is a hub docs-proposal commit, not `agent-quota-launcher` HEAD and not on published `16`.

These are publication defects. They are not converted into Agent Hub source defects.

## 1. What this audit checked

Read-only Git objects at named pins; compact 199/203 manifests; `5dc`→`7929` and `7929`→`16` diffs; APIs at `16`; Argon retained logs/scripts; F-POST-1/2 against host source and H3; current M4/M5/M7/M8 objects. No source/test/proposal edits, no push, no native/journal effects, no 453/635/89-PNG repeats.

Evidence (owner-relative): `agent-hub-wt-platform-postland-review/.local/review/`; this worktree `.local/audit/`; head `agent-coordination/.local/platform/` receipts named below.

## 2. Pins and digests (every full 40/64 hex in `c195` checked)

| Role | SHA | Status |
| :--- | :--- | :--- |
| Original report | `c1951e2c9f6a953cf876df9e5cdacb4cbfafbb39` | commit; parent `16`; only path vs `16` is this review file |
| Published `main` | `16d66d69708268ab84089447534f0439e0bfd893` | 209 tracked; parent `e5133fc9cf5e71a29fbfb900aaa3b0455984f8e5`; source bytes match `3888` except published Windows review annotation |
| Windows source | `3888a439f9e32d5a075a7e3d23b08ae9aae8201d` | ancestor of `16`; parent `a42ea0fd6daeb04a1bfc4de73478f557dcf02171` |
| CFB `main` land | `7929a1bc07bfea3a2b387989800cf1b0ba140984` | 202 tracked; first-parent `5a4b9381d4863d10a1d46b249c0be83e17cee18f` |
| Pre-conversation baseline | `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24` | 131 tracked |
| CFB freeze / review | `cfb4dc26985cbe39a1ae770ade4a6a57880edcbc` / `5a4b9381d4863d10a1d46b249c0be83e17cee18f` | commits; CFB parent `f9f6fb258e5abdf4d97e9292043828b4cc80ac82` |
| Component inputs | `8dc9235ce246…`, `ddaf80d82022…`, `eee4d2f88305…`, `d80dfebd806a…`, `0896ebe157cc…` | commits exist |
| Windows proposal/review | `2de0a6c1846b…`, `3d0dfadbed35…`, `e5133fc9cf5e…`, `0da55c0789c3…` | commits exist |
| Compact 199 at `7929` | `17aab235373ff0f98cce9ff361b74333d9d3bd23bf45790d471865f3ea02223a` | 199/199 blob SHA-256 match |
| Compact 203 at `16` | `5fb8eeace9f7b936e1b9043902cea3783cd0aa72decabedb51f96bef298e8804` | 203/203 match; 193 CFB paths byte-identical `7929`→`16` |
| CFB / Windows patches | `e8c91a9c3f3a795360c85eec300d3df9187a361d57f9a9dfc9312ad60e673d10` (1,366,991 B); `bf2f3df7070c2c9145ca86dd4ada7e5ff6399c6e478284807e0d1009b5d61500` (**58,763 B**, also `2de0` `WINDOWS-CONVERSATION.patch`) | match |
| Doc blob SHA-256s in `c195` | all nine remaining 64-hex (proposal/review/journal receipts) | match Git blobs or named head reconciliation JSON |
| Claimed M5 `0a4a2fb95467fa96518068f68f15c0c93e5301e6` | **MISSING** git object | |

Shared checkout `agent-hub` HEAD `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24`: 131/131 blobs unchanged (`hub-shared-checkout-preservation-after.json`; re-checked).

## 3. Changed paths (mechanical)

`5dc`→`7929`: **91** Git paths = **88** source + **3** meta (`docs/proposals/CONVERSATION-INTEGRATION.md|.patch`, `docs/reviews/CONVERSATION-INTEGRATION.md`).

**Not in that diff** (claimed in original §2.2): `docs/SPEC.md`, `host/tests/test_cli.py`, `package.json`, `public/index.html`, `tsconfig.json`, `vitest.config.ts` (exist, unchanged); `public/dashboard.js`, `public/styles.css`, `public/tokens.css`, `public/ui-kit.css`, `wrangler.toml` (absent at both pins). Actual extra worker file: `src/schema.ts`. Actual host seventh path: `host/tests/test_http_cli.py`. Public delta is 8 UI files + 22 `public/vendor/dakit/**` (lock + 21 locked files). `test/` delta is 34 files.

`7929`→`16`: **13** paths (10 host source/test + 3 Windows docs). Original §2.2 Windows path list and full file SHA-256s match. New files: `private_fs.py`, `win32_fs.py`, `test_private_fs.py`, `test_windows_imports.py`.

## 4. APIs at `16` vs original names

Present as named: `formatHumanActorFromSub` (`308`), `HUMAN_SUB_REGEX`, `createSqlIdentityValidator` (`354`), `requireActiveMembership` (`1819`), `collapsed_admin_identity_refused`, `csrf_invalid`, `invalid_principal_kind`, `cannot_remove_last_owner`, `member_removed`, `owner_required`, `invalid_approval`, `agent_machine_conflict`, `agent_exists`, `ambiguous_agent_tag`, `export { Hub as ConversationHub }`, `ConversationsService.retentionCleanup`, bus `retentionCleanup` wrap in `conversation-hub.ts` constructor, `run_cycles(..., enabled=False)` → `BridgeCapabilityGap(HOLD_REASON)`, 7-tuple journal identity, `qualify()` machine/hint checks (`113–132`), `resume_from_args` (`463`), F11 `listMessages` (`3021`/`3126`), F12 retained idempotency (`2784`), `deviceReview` project/tag copy (`ui-pages.ts:36`). Unique index is `idx_agents_project_tag_unique ON agents(COALESCE(project, ''), tag)`. DELETE/POST `/api/agents` live in `src/hub.ts:1001–1033`. Credential death on agent delete / `machine_id` change is SQL triggers `agents_revoke_credentials` and `agents_revoke_machine_binding`.

**Not present (do not use):** `ConversationHub.runRetentionCleanup`, `registerAgentPairing`, `revokeUnboundAgentCredentials`, `registerRegisteredAgent`, `agent_tag_ambiguous`, `agent_already_registered`, `idx_agents_projectless_tag`, `idx_credentials_registry_actor`. `requireActiveMembership` is real on `16`; original M4 “next step” text treated it as a future name.

## 5. Attributed Argon execution (logs, not re-run)

| Pin | Evidence | Log result |
| :--- | :--- | :--- |
| `7929` | worker probe | `4/4` P1–P4 |
| `7929` | host probe | `3/3` H1–H3 (H1 genuine read-only `qualify`, 0 journal/lock) |
| `7929` | UI probe | `2/2` U1–U2 (`locked_files:21`, `checked_ui_files:8`) |
| `7929` | `unittest` conversations + conversation_bridge | `Ran 50 tests` `OK` |
| `7929` | `existing-ui.checks` / `conversations-ui.checks` | PASS text |
| `16` | worker/host/UI probes | same labels `4/4`, `3/3`, `2/2` (repeats, not new cases) |
| `16` | private_fs + windows_imports + config + bridge | `Ran 90 tests` `OK (skipped=1)` — skip is `test_real_windows_reparse_refused_if_symlink_setup_supported` (`skipUnless os.name == 'nt'`) |
| `7929` | `typecheck.log` | **empty file**; silent `tsc` success is plausible, exit not independently recorded |

Retained attempt logs include `host-unit-subset-attempt1` `FAILED (errors=1)` then 50/50. Resource **receipt** `resources.json`: peak **7,774,208** allocated / **6,822,746** apparent; `memory.peak=432,181,248`; `pids.peak=43`. Pre-main 453/635/89-PNG counts stay attributed to `platform-conv-review` / `5a4b9381`; journal SHA-256s match head reconciliation JSON. This auditor created no native/journal effects.

## 6. F-POST-1 / F-POST-2 (Low; non-blocking per Argon)

**F-POST-1** (host CLI `machine_id`): **retained**. `api.safe_http_json` records binding only for `/api/device/token` when `machine_id is not None` (`api.py:74–80`). `record_authenticated_machine_binding` returns on `None` (`config.py:59–61`). `device_login` / `refresh_token` do not pass `machine_id` into `update_config`. H3 in `postland-host-probe.py` (mock HTTP) asserts stale `"old-machine"` after null token payload and dropped binding after refresh with a new id; logs PASS on `7929` and `16`. `ConversationBridge.qualify()` still fails closed on machine mismatch. Follow-up is bounded host-CLI polish for head to assign. No automatic migration and no new operator authorization.

**F-POST-2**: **retain the operational constraint, drop invented names.** In-place NULL→machine bind works in the approve transaction (`hub.ts:739–744`) and the `machine_id` trigger revokes that actor’s credentials. Changing an already non-null `machine_id` is `409 agent_machine_conflict`. `POST /api/agents` on an existing tag is `409 agent_exists`. `DELETE /api/agents/:actor` deletes the row (bare multi-project tag: `409 ambiguous_agent_tag`). Moving a bound agent therefore still needs DELETE + POST + re-pair. Agents table has no `workspace`/`session_id` columns; do not document those as registration fields. No admin rebind endpoint exists.

## 7. Roadmap (not on published `16`)

Full M1–M11+M2b remains active. User ops: no deploy/enrollment/services/Rust/native-CLI hold.

| Item | Actual object | On `16`? |
| :--- | :--- | :--- |
| M4 | Author `dcb27b74230da1b173a950ccbf264d4d1963ec76`; composed `1a15cb1513db147cd51f970e11edf5ad3df9abf3` (merge with `16`); 7 paths vs `16`; 207-file compact `96ea8ee548250fed85679407395064a8b60dc05fd45b3ae882fbd4686bec671a` | no (`3e134565` is not a commit) |
| M5 | Rejected `1229be75432dc55f9939135d1a14a7e6e6e6a04b` / `1fe94af0a605f50af9b8a2eec4da11ea92265379`; current `117f24045ec402ccbd1791c2815f4f24e5bd1d7e` under Grok review | no |
| M7 | Early `5546a29d4c0224b6770844e18ff09428cf1c55ad` (parent `16`); paths `docs/ROLE-LEASES.md`, `src/hub.ts`, `src/jobs.ts`, `src/role_leases.ts`; `jobs.py` not in that diff | no |
| M8 | Hub proposal `6ee79d6ae13265135b89ca87d37d503de0099b76` / Grok review `f194f4fdc227592829b1ceb87f95423977b638f4` (SCOPED protocol accept of that proposal; 560+4 attributed to `f194`, not this audit). Neither is on `16` nor in `agent-quota-launcher`. Live backend-death / account oracle / native first-action unproven. No 3-account requirement. Older `f5aed5c87cfccd42caed78fa870c1acdd5651c44` remains disputed. | no |

Physical Windows / live Cloudflare / Cognito / two-host adoption remain separate. Next owners: head assigns F-POST-1 host-CLI follow-up and M4/M5/M7/M8a/b after their own reviews; Windows physical owner for `M3b`/`M11`; `hub-zai` keeps shared-checkout ops.

## 8. Limits

No replacement Argon source verdict. No physical delivery. `typecheck.log` has no recorded exit. Parallel-task SHAs above are object checks, not reviews by this identity. Combined scratch cap 12 MiB; this audit `.local/audit`+`.local/tmp` measured under the 8 MiB early guard (see `.local/audit/postland-factual-audit.json`).
