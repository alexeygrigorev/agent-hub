# Hub Engine Selection & Burn-Rate Decision Logic

This document defines the deterministic decision function that Agent Hub uses to choose an engine, subscription account, profile, model, and host when launching a session (`FleetService.plan` / `evaluateEngineDecision`), as well as the policy governing callers that pin an engine (`--engine <engine> --reason <reason>`).

## 1. Guiding Principles (Alexey, 2026-11-02)

- **Uniform burn-rate pacing across subscriptions:** Every subscription is paid for and should be burned to its limit uniformly before its reset window (`5h`, `7d`/weekly, or `monthly`), rather than burning one engine (`claude`) to zero while others sit idle:
  - *«I'd rather have hub have a good decision logic for selecting the engine. have argon think about it and implement.»*
  - *«let's focus on using muse antigravity and zai and codex more than claude - we can clearly see that the usage is draining faster yet hub picks up claude for some reasons»*
  - *«I don't see the single muse»*
  - *«it should do it uniformly not just burn one engine to 0 while the rest aren't used»*
- **Single-account discipline & profile mappings:**
  - `zai` is **one** account (`zai-central`, plus its alias `shared-zai`), accessed either via OpenCode (`glm-flash`, profile `hub-zai-central`) or Codex CLI (`glm-flash-zcodex`, profile `zcodex`).
  - `antigravity` = `gemini` (`argon` on `hub-antigravity` / `argon-gemini` on `shared-gemini`).
  - `muse` = `opencode` via managed profile `hub-shared-go` (`opencode-go/muse-spark-1.3-contributor` on account `shared-go`, provider `go`).
  - `codex` does **not** run on the Windows desktop (`DESKTOP-FP4OP26` / `0sJC0fDcz0bzhG4XQp5EeQ`) due to the `codex-vpn` guard (`desktop_codex_excluded`). Note that `zcodex` (`provider: zai`) uses the Z.ai endpoint rather than OpenAI and remains eligible on desktop.
- **Explicit override policy:** Callers (operators, skills, briefs, routines, refill loops) must **not** hard-code `--engine claude`. Automatic selection is the default. If an operator or caller pins `--engine <engine>`, `--reason <non-empty text>` (`engine_override_reason`) is required; otherwise Hub rejects the request with `400 engine_reason_required`. Even when pinned with a valid reason, hard eligibility gates (exhausted quota `<= reserve_percent`, `limit_reached`, `cooldown_active`, expired auth, desktop `codex` guard, concurrency ceiling) remain enforced.

---

## 2. Decision Table

Every `(machine, route)` candidate passes through **Phase A (Hard Eligibility Gates)** followed by **Phase B (Multi-Factor Score)** and **Phase C (Deterministic Tie-Break)**.

### Phase A: Hard Eligibility Gates (Binary Exclude)

| Gate | Input / Condition | Rejection Reason (`reason`) | Queue Behavior |
| :--- | :--- | :--- | :--- |
| Host online & role | Machine offline, draining, or not `worker` | `machine_offline` / `machine_draining` | Permanent |
| Host resources | Memory < reserve + session RAM, disk < floor, swap high | `memory_pressure` / `disk_low` / `swap_pressure` | Capacity hold (`202`) |
| Workspace presence | Required workspace missing on host | `workspace_missing` | Permanent |
| Desktop `codex-vpn` guard | Host is desktop (`DESKTOP-FP4OP26` / `0sJC0fDcz0bzhG4XQp5EeQ`) and effective provider is `codex` (OpenAI) | `desktop_codex_excluded` | Incidental (other hosts tried; capacity hold on Linux hosts stays `202`) |
| Engine / account / profile filter | Pinned `engine`, `account`, or `profile` does not match candidate | `engine_mismatch` / `account_mismatch` / `profile_mismatch` | Permanent |
| Task tier & capability fit | Candidate route does not match any model in requested `tier` (`deep`, `daily`, `quick`, `review`, `ask` or aliases) or lacks `needs` (`long_context`, `fast`, `cheap`, `tool_use`) | `tier_unavailable` / `model_policy_excluded` | Permanent |
| Subscription auth health | `auth_status === 'expired'` or missing live probe | `engine_auth_expired` / `account_unverified_by_live_probe` | Incidental |
| Quota exhaustion & reserve | `remaining_pct <= reserve_percent` (15% for metered pools; 0% when `limit_reached`), or `cooldown_active` | `quota_exhausted` / `quota_below_reserve` / `account_cooldown` | Capacity hold (`202`) |
| Account & engine concurrency | Active + reserved + uncertain sessions on account $\ge$ `max_concurrency` | `account_concurrency` / `engine_concurrency` | Capacity hold (`202`) |

### Phase B: Soft Scoring Factors (Multiplicative Score $S$)

For every eligible `(machine, route)` matched to policy model $m$, Hub computes:

$$S = H \times B_{\text{burn}} \times P_{\text{pref}} \times F_{\text{tier}} \times L_{\text{load}} \times E_{\text{fail}} \times M_{\text{host}} \times W_{\text{tbm}}$$

| Symbol | Factor Name | Formula / Definition | Purpose |
| :--- | :--- | :--- | :--- |
| $H$ | **Remaining Headroom** | $H = \frac{Q_{\text{rem}}}{100}$, where $Q_{\text{rem}} \in [0, 100]$ is the bottleneck remaining % across active quota windows (minus reserve floor in `modelCandidates`). | Prefers accounts with higher raw remaining percentage. |
| $B_{\text{burn}}$ | **Burn-Rate Schedule Factor** | For each active window $w \in \{\text{5h}, \text{7d}, \text{monthly}\}$ with remaining quota fraction $q_w = Q_w / 100$ and remaining time fraction $\tau_w = \max(0.05, \min(1, \frac{t_{\text{reset},w} - t_{\text{now}}}{T_w}))$, the pace ratio is $r_w = \frac{q_w}{\tau_w}$. Let $r = \min_w r_w$ be the bottleneck pace ratio:<br>• $r > 1.0$: **behind burn schedule** (more % left than time left) $\implies B_{\text{burn}} = \min(2.5, r)$<br>• $r = 1.0$: **on schedule** $\implies B_{\text{burn}} = 1.0$<br>• $r < 1.0$: **ahead of burn schedule** (burning too fast) $\implies B_{\text{burn}} = \max(0.05, r^2)$, and if depleted ($Q_{\text{rem}} < 30\%$), additionally multiplied by $\left(\frac{Q_{\text{rem}}}{30}\right)^2$. | Core uniform-pacing mechanism: boosts accounts behind schedule (`muse`, `antigravity`, `codex`, `zai`) and sharply dampens accounts burning faster than their reset clock, especially below 30% (`claude`). |
| $P_{\text{pref}}$ | **Provider & Project Preference** | Base provider weight from `config/model-routing.json`:<br>• `go` (`muse`): `1.30`<br>• `gemini` (`antigravity`): `1.30`<br>• `zai` (`zai-central`): `1.30`<br>• `codex` (OpenAI): `1.15`<br>• `copilot`: `1.00`<br>• `claude`: `0.75`<br>• `grok`: `0.20`<br>Multiplied by optional per-project weight (`rules.project_preferences[project][provider]`, default `1.0`). | Implements *«focus on using muse antigravity and zai and codex more than claude»* even when burn ratios are close. |
| $F_{\text{tier}}$ | **Tier & Capability Fit** | Base `1.0`, adjusted by:<br>• Tier position bonus: $1 + 0.02 \times \max(0, 6 - \text{tier\_index})$<br>• Capability fit for `needs` (`tool_use`, `long_context`, `fast`, `cheap`) and `tier` (`deep` boosts 1M-context/deep-reasoning engines; `quick` boosts high-speed engines). | Matches task requirements (tool use, context window, speed) while preserving tier ordering as a gentle preference rather than a hard lock onto index 0. |
| $L_{\text{load}}$ | **Live Session Concurrency & Slot Headroom** | Let $a_{\text{acct}}$ be live + reserved sessions on the specific account, $a_{\text{fam}}$ be live sessions across the provider family, and $u_{\text{slot}} = \frac{\text{available\_slots}}{\text{max\_concurrency}} \in (0, 1]$:<br>$$L_{\text{load}} = \frac{0.5 + 0.5 \cdot u_{\text{slot}}}{(1 + 0.35 \cdot a_{\text{acct}}) \cdot (1 + 0.5 \cdot a_{\text{fam}})}$$ | Spreads concurrent sessions across accounts and engines so a heavily loaded family (`claude` with 15/50 live sessions) loses to idle/lightly loaded accounts (`muse` with 0 live sessions). |
| $E_{\text{fail}}$ | **Recent Account Failure / Exhaustion Penalty** | Let $f_{1\text{h}}$ be the count of launch failures or quota/auth refusals on the account in the last 60 minutes, plus `1` if `degraded`:<br>$$E_{\text{fail}} = \frac{1}{1 + 0.5 \cdot f_{1\text{h}}}$$ | Avoids repeatedly hammering an account that recently failed or hit transient rate limits. |
| $M_{\text{host}}$ | **Host Load Factor** | $M_{\text{host}} = \frac{1}{1 + \text{busy\_cores}/\text{cpu\_count} + \text{pending\_launches}}$ | Balances CPU and launch queue across eligible hosts (`hetzner`, `tabo`, `desktop`). |
| $W_{\text{tbm}}$ | **Learned TBM Weight** | Historical completion/value weight $\in [0.25, 2.0]$ (default `1.0`). | Incorporates observed task completion efficiency. |

### Phase C: Deterministic Tie-Break Order

When two eligible candidates have scores within floating-point tolerance (or identical scores):
1. Non-overflow pool before overflow pool (`!a.overflow` before `b.overflow`).
2. Non-degraded route before degraded route (`!degraded.has(a)`).
3. Higher composite score $S$ (`b.score - a.score`).
4. Higher burn-rate pace ratio $r$ (`b.decision.factors.burn_ratio - a.decision.factors.burn_ratio`).
5. Higher raw remaining quota percentage $Q_{\text{rem}}$ (`b.decision.factors.remaining_pct - a.decision.factors.remaining_pct`).
6. Fewer active sessions on the account (`a.decision.factors.active_account - b.decision.factors.active_account`).
7. Lexicographical order of `(machine, engine, account, model_id)`.

---

## 3. Worked Examples (2026-11-02 17:15Z Live Fleet Snapshot)

### Measured Inputs (2026-11-02 17:15Z, `hub quota`)

Suppose an unpinned `daily` session is requested on Hetzner (`busy_cores = 0`, `pending = 0`, $M_{\text{host}} = 1.0$, $W_{\text{tbm}} = 1.0$, $E_{\text{fail}} = 1.0$). Across the fleet, 15 of 50 live sessions are on `claude`. Almost 60% of the weekly window remains ($\tau_{\text{7d}} = 0.60$, i.e. 100.8h left of 168h) and 80% of the 5h window remains ($\tau_{\text{5h}} = 0.80$, i.e. 4.0h left of 5h).

| Candidate (`engine / account / model`) | Provider | $Q_{\text{rem}}$ | Window $\tau_w$ | Pace Ratio $r = q_w / \tau_w$ | Depleted ($<30\%$)? | Burn Factor $B_{\text{burn}}$ | Pref $P_{\text{pref}}$ | Live Load ($a_{\text{acct}}, a_{\text{fam}}$, slots) | $L_{\text{load}}$ | Tier Fit $F_{\text{tier}}$ | Final Score $S$ | Outcome |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **`antigravity / hub-antigravity (argon)`** | `gemini` | **60%** | `7d` ($\tau=0.60$) | $0.60 / 0.60 = \mathbf{1.00}$ | No | **1.000** | **1.30** | $a_{\text{acct}}=0, a_{\text{fam}}=1$, $6/6$ free | $0.667$ | $1.10$ | **0.5720** | **#1 Winner (or #1 on `deep`)** |
| **`opencode / shared-go (muse)`** | `go` | **47%** | `5h` ($\tau=0.40$*) or `7d` ($\tau=0.60$) | At $\tau=0.40$: $0.47/0.40 = \mathbf{1.18}$<br>At $\tau=0.60$: $0.47/0.60 = \mathbf{0.78}$ | No | **1.175** (or **0.614**) | **1.30** | $a_{\text{acct}}=0, a_{\text{fam}}=0$, $4/4$ free | $1.000$ | $1.12$ | **0.8048** (at $\tau=0.40$)<br>**0.4204** (at $\tau=0.60$) | **#1 Winner when behind schedule** |
| **`codex / shared-codex (astra)`** | `codex` | **54%** | `7d` ($\tau=0.60$) | $0.54 / 0.60 = \mathbf{0.90}$ | No | **0.810** | **1.15** | $a_{\text{acct}}=1, a_{\text{fam}}=3$, $5/6$ free | $0.272$ | $1.04$ | **0.1423** | Eligible on Linux; **excluded on Desktop** (`desktop_codex_excluded`) |
| **`opencode / zai-central (glm-flash)`** | `zai` | **31%** | `5h` ($\tau=0.25$*) or `7d` ($\tau=0.60$) | At $\tau=0.25$: $0.31/0.25 = \mathbf{1.24}$<br>At $\tau=0.60$: $0.31/0.60 = \mathbf{0.52}$ | No ($31\% \ge 30\%$) | **1.240** (or **0.267**) | **1.30** | $a_{\text{acct}}=0, a_{\text{fam}}=1$, $6/6$ free | $0.667$ | $1.08$ | **0.3601** (at $\tau=0.25$)<br>**0.0775** (at $\tau=0.60$) | **Picked when 5h window is near reset ($r=1.24$)** |
| **`claude / hub-claude (sonnet)`** | `claude` | **15%** | `7d` ($\tau=0.60$) | $0.15 / 0.60 = \mathbf{0.25}$ | **Yes ($15\% < 30\%$)** | $0.25^2 \times (15/30)^2 = 0.0156 \to \mathbf{0.050}$ (floor) | **0.75** | $a_{\text{acct}}=7, a_{\text{fam}}=15$, $1/8$ free | $0.019$ | $1.04$ | **0.0001** (or excluded at 15% reserve gate) | **Loses by >3,000x (and blocked at 15% reserve floor)** |

*\*Note on 5h vs 7d windows:* When an account's 5-hour window is close to resetting (e.g., $\tau_{\text{5h}} = 0.25$, 1.25 hours left before reset) while 31% (`zai-central`) or 47% (`muse`) of that window remains, its pace ratio is $r > 1.0$ (**behind burn schedule**), so $B_{\text{burn}} = r > 1.0$ boosts `muse` and `zai-central` to the top of the queue so their expiring 5-hour quota is used before reset instead of wasted.

### Why `claude` at 15% Always Loses to `antigravity` at 60%, `muse` at 47%, `codex` at 54%, and `zai` at 31%

1. **Reserve Gate:** At exactly `15%` remaining with `reserve_percent = 15`, `claude` hits `quota_below_reserve` (`15 <= 15`) and is excluded from normal placement.
2. **Depletion & Burn-Rate Dampening (even at 16%–29%):** Suppose `claude` is at `20%` remaining with 60% of its weekly window left ($r = 0.20 / 0.60 = 0.333$). Because $Q_{\text{rem}} = 20\% < 30\%$, the depletion multiplier $(20/30)^2 = 0.444$ compounds with $r^2 = 0.111$, clamping $B_{\text{burn}}$ to the `0.05` floor (`20x` penalty vs on-schedule `antigravity`), before applying the `0.75` `claude` provider weight vs `1.30` for `muse`/`antigravity`/`zai` and the `15`-session family load penalty.

---

## 4. Visibility & CLI / API Surfaces

### 4.1 Logged `decision_reason` in `hub sessions show` and `hub sessions start`

Every planned or reserved session records:
- `decision_score`: numeric composite score $S$ (rounded to 4 decimals).
- `decision_reason`: human-readable single-line explanation of why the engine/account/model was chosen, e.g.:
  - Automatic selection:
    `auto: opencode/shared-go (muse) on hetzner score=0.4204 [rem=47% win=7d@100.8h ratio=0.78 burn=0.61x pref=1.30x tier=daily(1.12x) load=acct:0,fam:0,slots:4/4]`
  - Pinned override with reason:
    `pinned: claude/hub-claude (sonnet) on hetzner score=0.0124 [override_reason="reproduce claude-specific parser bug"; rem=22% depleted win=7d@100.8h ratio=0.37 burn=0.07x pref=0.75x]`
- `decision`: structured breakdown (`mode`, `score`, `reason`, `override_reason`, `factors`: `{ remaining_pct, window_type, hours_to_reset, burn_ratio, burn_factor, depleted, provider_weight, tier_fit, load_factor, failure_factor, host_factor, tbm_weight, active_account, active_family, available_slots, max_concurrency, recent_failures }`).

### 4.2 Burn Status & Decision Score in `hub quota`

`GET /api/quota` and `hub quota` enrich every subscription pool with:
- `burn_ratio`: pace ratio $r = q_w / \tau_w$ against the bottleneck reset window
- `burn_factor`: multiplier $B_{\text{burn}} \in [0.05, 2.5]$
- `burn_status`: `behind_schedule` ($r > 1.05$), `on_pace` ($0.90 \le r \le 1.05$), `ahead_of_schedule` ($r < 0.90$ and $Q_{\text{rem}} \ge 30\%$), `depleted` ($Q_{\text{rem}} < 30\%$), `exhausted` ($Q_{\text{rem}} \le \text{reserve}$ or `limit_reached`), or `unmetered`
- `decision_score` and `decision_reason`: preview placement score and reason for a baseline `daily` session on that pool.

### 4.3 `hub engines recommend` (`GET /api/fleet/recommend` / `GET /api/engines/recommend`)

Callers (skills, routines, briefs, refill loops, operators) that want to inspect or use Hub's recommendation without hard-coding `claude` can either omit `--engine` when calling `hub sessions start` or query:

```bash
hub engines recommend [--tier daily|deep|quick|review|ask] [--workspace repo] [--machine host] [--project proj]
```

This endpoint runs the exact `FleetService.plan()` decision function without creating a reservation and returns the winning `(engine, account, profile, model_id, machine, decision_score, decision_reason)` along with the ranked candidate list and any excluded candidates with their exclusion reasons.
