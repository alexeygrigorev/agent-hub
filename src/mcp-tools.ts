/** Fixed API routes only. Every call re-enters the ordinary Hub HTTP auth path. */
type Method = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
export interface ToolRoute { name: string; description: string; method: Method; path: string; cli: string[]; }
const routes: ToolRoute[] = [];
function tool(name: string, method: Method, path: string, description: string, cli: string[] = []) {
  routes.push({ name, method, path, description, cli });
}
tool('repos_list', 'GET', '/api/repos', 'Private repository registry; query org, status (active/archived/duplicate), flagged=true.', ['repos list']);
tool('repos_show', 'GET', '/api/repos/{name}', 'Repository metadata and canonical duplicate target.', ['repos show']);
tool('repos_edit', 'PATCH', '/api/repos/{name}', 'Operator-only human fields: payload label, aliases, purpose, stack, key_folders, urls. Refresh never overwrites these.', ['repos edit']);
tool('status', 'GET', '/api/status', 'Authorized status; query accepts since, task, project, host, after, limit.', ['status']);
tool('machines_available', 'GET', '/api/machines/available', 'List available machines.', ['machines available']);
tool('fleet', 'GET', '/api/fleet', 'Fleet machines, native sessions, agents, accounts and launch lineage.', ['fleet']);
tool('resources', 'GET', '/api/fleet/resources', 'Resource utilization, placement settings, measured session statistics and recent watchdog reaps.', ['resources']);
tool('resource_sessions', 'GET', '/api/fleet/resources/sessions', 'Durable per-session resource ledger; query filters project, engine and kind.', []);
tool('resource_reaps', 'GET', '/api/fleet/resources/reaps', 'Confirmed watchdog reaps with actor, time, reason, freed memory and intake linkage.', []);
tool('quota', 'GET', '/api/quota?view=fleet', 'Quota usage and reset evidence; query filters match hub quota.', ['quota']);
tool('usage_history', 'GET', '/api/monitoring/history?kind=quota_window', 'Shared persisted usage history used by the dashboard and TBM; query range=24h/7d/30d, kind=quota_window/quota/tokens, machine optional. Unknown points stay gaps.', ['quota history']);
tool('auth_status', 'GET', '/api/auth/me', 'Safe human login identity (human credentials only).', ['auth status']);
tool('heartbeat', 'POST', '/api/heartbeat', 'Report own agent liveness (health:report scope).', ['heartbeat']);
for (const [name, mode] of [['start_agent', 'headless'], ['start_session', 'interactive'], ['plan_agent', 'plan']] as const) {
  tool(name, 'POST', mode === 'plan' ? '/api/fleet/plan' : '/api/fleet/launches', `Legacy fleet ${mode}: provide the same JSON as the CLI; placement/admission unchanged.`, [name.replaceAll('_', '-')]);
}
tool('sessions_link', 'POST', '/api/sessions/{id}/link', 'Operator manual intake linkage; payload key, intake_task_id. Existing different linkage is refused.', ['sessions link']);
tool('sessions_list', 'GET', '/api/sessions', 'List sessions; query view=lifecycle gives grouped lifecycle; state, limit, cursor filter.', ['sessions', 'sessions list']);
tool('sessions_tree', 'GET', '/api/sessions/tree', 'Session requester/parent tree.', ['sessions tree']);
tool('sessions_start', 'POST', '/api/sessions', 'Queue durable session intent: workspace, brief, name, mode=headless|interactive, parent_id, intake_task_id, key, with_goal. Omit engine for tier policy; explicit engine REQUIRES engine_override_reason. Non-interactive starts REQUIRE intake_task_id and nonempty AC. Scope flags are informational only and never hold a start. needs filters advertised machine labels; unmet needs hold with needs_unmet. Quota/admission unchanged. Use interactive with_goal for follow-ups; headless uses native resume. Poll sessions_show/commands: queued or transported is not acknowledged execution.', ['sessions start', 'sessions override']);
tool('sessions_show', 'GET', '/api/sessions/{id}', 'Session binding and observed state.', ['sessions show']);
tool('sessions_commands', 'GET', '/api/sessions/{id}/commands', 'Read command results; recorded/transported is not acknowledged execution.', ['sessions commands']);
for (const action of ['send', 'capture', 'stop']) tool(`sessions_${action}`, 'POST', `/api/sessions/{id}/${action}`,
  action === 'capture' ? 'Queue a sanitized capture; payload: key, max_bytes, screen. Poll sessions_commands for the result.' :
  `DESTRUCTIVE: queue ${action}; payload: key${action === 'send' ? ', body' : ''}. Never infer completion from enqueue; poll sessions_commands.`, [`sessions ${action}`]);
tool('sessions_cancel', 'POST', '/api/sessions/{id}/cancel', 'Cancel an owned held, queued or unclaimed offered intent before start authorization. payload: key. Reuse the stable key; the no_native_start receipt is cancellation, not confirmed native stop.', ['sessions cancel']);
tool('sessions_protect', 'POST', '/api/sessions/{id}/protect', 'Permanently protect an owned session from automated cleanup. payload: key. No unprotect or native effect. Exact current owner credential family is required.', ['sessions protect']);
tool('sessions_wake', 'POST', '/api/sessions/{id}/wake', 'Durable verified-send wake. Payload: key, at (ISO or now), prompt. Future wake suppresses stall recovery; missing ack after 10m opens attention and intake note. Read history via sessions_show.', ['sessions wake']);
tool('sessions_cleanup', 'POST', '/api/sessions/cleanup', 'DESTRUCTIVE when dry_run=false: plan/apply cleanup through existing custody guards. Explicit payload dry_run=true for a preview.', ['sessions cleanup']);
tool('sessions_observe', 'POST', '/api/sessions/observations', 'Host-only lifecycle observation; ownership remains server validated.', ['sessions observe']);
for (const action of ['send', 'reply']) tool(`bus_${action}`, 'POST', `/api/bus/${action}`, 'DESTRUCTIVE: send a durable message using payload from_actor, to_actor, kind, body, idempotency_key and optional thread_id, reply_to, lease_epoch' + (action === 'send' ? '; to_actor session:<id> or tag@machine types into a session on any host' : '') + '.', action === 'send' ? [action, 'msg send'] : [action, 'msg reply']);
tool('bus_inbox', 'GET', '/api/bus/inbox', 'Inbox query: actor, limit, cursor.', ['inbox', 'msg inbox']);
tool('bus_show', 'GET', '/api/bus/messages/{id}', 'Read a message and its delivery state.', ['show', 'msg show', 'msg status']);
for (const stage of ['ack', 'accept', 'complete']) tool(`bus_${stage}`, 'POST', `/api/bus/messages/{id}/${stage}`, `Explicitly ${stage} a message; optional payload lease_epoch.`, [stage]);
tool('msg_send', 'POST', '/api/bus/send', 'DESTRUCTIVE: send a durable message: payload to_actor (session:<id>, tag@machine, agent:<name>, human:<sub>), body, idempotency_key (retry with the SAME key); optional delivery tui|inbox, deliver_by_s, thread_id, ref, as (delegate-checked named agent).', ['msg send', 'send']);
tool('msg_reply', 'POST', '/api/bus/reply', 'DESTRUCTIVE: reply in a thread: payload reply_to, body, idempotency_key; optional from_actor/to_actor, delivery, ref, as (delegate-checked).', ['msg reply', 'reply']);
tool('msg_inbox', 'GET', '/api/bus/inbox', 'Inbox long poll: query actor (or as), unread=1, thread, wait 0..60 s in one bounded request, limit, cursor. Reading marks delivered.', ['msg inbox', 'inbox']);
tool('msg_show', 'GET', '/api/bus/messages/{id}', 'Read one message; the recipient view marks it read. Query as optional (delegate-checked).', ['msg show', 'show']);
tool('msg_ack', 'POST', '/api/bus/messages/{id}/ack', 'DESTRUCTIVE: acknowledge as the recipient; payload lease_epoch, as (delegate-checked) optional.', ['msg ack', 'ack']);
tool('msg_status', 'GET', '/api/bus/messages/{id}/status', 'Delivery state, stage times and metadata-only audit events (no body). Query as optional.', ['msg status']);
tool('claims_list', 'GET', '/api/bus/claims', 'Claims query: project, holder, all.', ['claims']);
tool('bus_activity', 'GET', '/api/bus/activity', 'Work-activity visibility: active claims plus recent bus messages for a workspace, project, holder or actor. Query: workspace, project, holder, actor, limit, all. Read-only; shows whether someone is already on this before starting overlapping work.', ['activity']);
tool('claims_create', 'POST', '/api/bus/claims', 'Claim a scope using project, scope, holder, ttl_ms, lease_epoch.', ['claim']);
tool('claims_release', 'POST', '/api/bus/claims/{id}/release', 'Release a claim; id may be project:scope (encoded as one segment).', ['release']);
tool('tasks_submit', 'POST', '/api/fleet/tasks', 'Submit authorized work; payload prompt, approved_action, needs, workspace, idempotency_key and routing preferences, or tasks batch.', ['submit']);
tool('routing_machines', 'GET', '/api/fleet/routing/machines', 'Capability and model advertisements.', ['routing-machines']);
tool('model_policy', 'GET', '/api/fleet/routing/policy', 'Configured model policy.', ['model-policy']);
tool('jobs_list', 'GET', '/api/fleet/tasks', 'Recent routed tasks.', ['jobs']);
tool('jobs_show', 'GET', '/api/fleet/tasks/{id}', 'Task state, report and artifacts.', ['job']);
tool('task_result', 'POST', '/api/fleet/tasks/{id}/result', 'Assigned host only: status/report/artifacts/confirmed_exit.', ['task-result']);
tool('artifact', 'GET', '/api/fleet/tasks/{id}/artifacts/{name}', 'Fetch a sanitized PNG as the API base64 DTO.', ['artifact']);
tool('intake_create', 'POST', '/api/intake/tasks', 'File verbatim task: payload key, project, title, text, transcription, source {type,reference,timestamp}, optional acceptance_criteria (nullable bounded text; required non-empty before assign/start).', ['intake create']);
tool('intake_list', 'GET', '/api/intake/tasks', 'One revision-pinned page: query status, project, archived=true (archived only), all=true (include archived), after, revision, authorization_valid_until, limit. Default hides archives. limit accepts a positive integer or numeric string, defaults to 25, clamps above max 100; response effective_limit states the applied size. Follow next as after with snapshot_version as revision and non-null authorization_valid_until from the first page; restart on stale_snapshot. CLI hub intake list --all follows all pages automatically.', ['intake list']);
tool('intake_forgotten', 'GET', '/api/intake/forgotten', 'Authorized forgotten open intakes: id, title, age_ms, last_session, state, reason, restart queue; optional project query.', ['intake forgotten']);
tool('intake_audit', 'GET', '/api/intake/tasks/{id}/audit', 'Append-only lifecycle audit for full id or prefix; survives task row.', ['intake audit']);
tool('intake_audit_recent', 'GET', '/api/intake/audit', 'Recent authorized lifecycle events; query limit, after, revision.');
for (const action of ['show', 'history', 'links', 'context']) tool(`intake_${action}`, 'GET', `/api/intake/tasks/{id}${action === 'show' ? '' : '/' + action}`, `Read task ${action}.`, [`intake ${action}`]);
for (const action of ['continue', 'update', 'append', 'assign']) tool(`intake_${action}`, 'POST', `/api/intake/tasks/{id}${action === 'update' ? '' : '/'}${action === 'append' ? 'events' : action === 'update' ? '' : action}`, `Task ${action}: same payload as CLI. ${action === 'update' ? 'key is generated if omitted; omitted expected_version is fetched with bounded CAS retry. Explicit versions never retry. done requires a DONE note containing commit SHA or deploy run URL AND Live check: command/URL plus Result: output; cancellation requires reason plus superseded_by or reason containing dropped by Alexey. update accepts acceptance_criteria (nullable; null clears). Changing project/title/acceptance_criteria (or appending intake text) re-hints scope and invalidates prior scope decisions.' : action === 'assign' ? 'Requires key and expected_version. Refused with acceptance_criteria_required when the task has empty acceptance criteria. The scope flag is informational and never refuses assign.' : action === 'append' ? 'Requires key and expected_version. scope_decision events are operator-only (verdict in_scope/gray_adr/out_of_scope/wrong_project); task text cannot approve itself.' : 'Requires key and expected_version.'} Append payload: {key, expected_version, note: {text, source?}} or {key, expected_version, event: {kind, ...}}. Event kind note accepts text and optional source; kind session accepts native_id or hub_session_id and optional provenance; session references keep link ownership checks.${action === 'append' ? ' Operator external registration: event {kind:"session",native_id:"FULL_UUID",host:"MACHINE_ID",verify_live:true}; fresh host census required, not launch proof.' : ''}${action === 'assign' ? ' Assign queues a real Hub session and returns placement hold reasons.' : ''}${action === 'update' ? ' done refuses worktree_close_gate; force+reason overrides.' : ''}`, action === 'update' ? ['intake update', 'intake close'] : [`intake ${action}`]);
tool('worktree_list', 'GET', '/api/intake/tasks/{id}/worktrees', 'Intake worktrees and close_gate.', ['worktree list']);
tool('worktree_write', 'POST', '/api/intake/tasks/{id}/worktrees/{action}', 'action register {key, worktree: {machine, path, branch?}} | refresh | remove {key, worktree_id?}.', ['worktree register', 'worktree refresh', 'worktree remove']);
for (const group of ['rules', 'grants']) {
  tool(`intake_${group}`, 'GET', `/api/intake/${group}`, `List operator ${group}.`, [`intake ${group}`]);
  tool(`intake_${group}_write`, 'POST', `/api/intake/${group}`, `Operator-only ${group} mutation; payload matches web API.`);
}
tool('intake_revoke', 'POST', '/api/intake/grants/{id}/revoke', 'Revoke a grant, preserving history.', ['intake revoke']);
tool('intake_grantees', 'GET', '/api/intake/grantees', 'Operator only: discover active grantable agent credential metadata for an actor: query actor.', ['intake grantees']);
tool('handoff_page', 'GET', '/api/intake/handoff', 'One snapshot-pinned handoff page. Follow next with revision and authorization_valid_until; never treat a page as a complete handoff.', ['handoff']);
for (const [name, path] of [['list', ''], ['participants', '/participants'], ['get', '/{id}'], ['members', '/{id}/members'], ['history', '/{id}/messages']] as const)
  tool(`conversations_${name}`, 'GET', '/api/conversations' + path, 'Read authorized conversations; query matches CLI filters/pagination.', [`conversations ${name}`]);
for (const [name, path] of [['create', ''], ['send', '/{id}/messages'], ['read', '/{id}/read'], ['members_add', '/{id}/members'], ['members_remove', '/{id}/members/remove']] as const)
  tool(`conversations_${name}`, 'POST', '/api/conversations' + path, `${name === 'send' ? 'DESTRUCTIVE: ' : ''}Conversation ${name}; payload matches the CLI/web API; author identity is server-derived.`, name.includes('members') ? [] : [`conversations ${name}`]);
for (const [name, path] of [['agents', '/api/agents'], ['liveness', '/api/liveness'], ['checks', '/api/checks'], ['metrics', '/api/metrics']] as const)
  tool(name, 'GET', path, `Read authorized ${name} dashboard data.`, name === 'metrics' ? ['metrics get'] : []);
tool('checks_write', 'PUT', '/api/checks', 'Operator-only scheduled checks configuration.');
tool('checks_run', 'POST', '/api/checks/{id}/run', 'DESTRUCTIVE: run an existing check and its configured actions.');
for (const action of ['archive', 'unarchive']) tool(`conversations_${action}`, 'POST', `/api/conversations/{id}/${action}`, `Conversation ${action}; existing member permissions apply.`);
tool('conversations_links_search', 'GET', '/api/conversations/links', 'Find linked conversations using the existing web API query.');
tool('conversations_links', 'GET', '/api/conversations/{id}/links', 'List linked task, session and artifact references.', ['conversations links']);
tool('conversations_follow_through', 'GET', '/api/conversations/{id}/follow-through', 'Read typed conversation follow-through authority facts and obligations.', ['conversations follow-through']);
tool('conversations_link_show', 'GET', '/api/conversations/{id}/links/{link_id}', 'Read one authorized link reference.');
tool('conversations_link_create', 'POST', '/api/conversations/{id}/links', 'Attach a link using payload kind, ref and idempotency_key.');
tool('conversations_link_remove', 'DELETE', '/api/conversations/{id}/links/{link_id}', 'Remove a conversation link with existing membership/ownership checks.');
tool('conversations_automation', 'PUT', '/api/conversations/{id}/automation', 'Operator-only configure an existing conversation automation grant.');
tool('conversations_automation_remove', 'DELETE', '/api/conversations/{id}/automation', 'Operator-only revoke conversation automation.');
tool('fleet_claim_policy', 'GET', '/api/fleet/claim-policy', 'Read configured workspace claim-admission policy.');
tool('fleet_claim_policy_write', 'PUT', '/api/fleet/claim-policy', 'Operator-only claim-admission policy update.');
tool('monitoring_disk', 'GET', '/api/monitoring/disk', 'Authorized disk monitoring dashboard.');
tool('monitoring_health', 'GET', '/api/monitoring/health', 'Authorized host health monitoring dashboard.');
tool('accounts_write', 'PUT', '/api/fleet/accounts', 'Operator-only account registry update.');
export const MCP_ROUTES = routes;
export const CLI_EXCLUSIONS: Record<string, string> = {
  'host-release': 'Operator release pin for a checkout-installed host; deliberate CLI step, not an agent tool.',
  'msg agent-credential': 'Writes a new private local credential file; MCP cannot write host files.',
  'msg agent-delegate': 'Operator grant of named-agent delegation to an MCP client; deliberate CLI step.',
  'msg readdress': 'Operator re-address of an undelivered session message; deliberate CLI step.',
  'msg whoami': 'Reports the local CLI identity source; MCP callers are identified by their own login.',
  'repos seed': 'Local-only parser reads the private project map and submits bounded seed batches.',
  'repos refresh': 'Local-only scanner invokes git/gh on host directories; MCP cannot scan the host filesystem.',
  'sessions warnings': 'Long-running warning display is CLI-only by product scope.',
  'sessions audit': 'Idle-audit action log and manual run are operator oversight; the Hub scheduler owns execution and the sessions UI plus hub sessions audit CLI own the display.',
  'sessions ack': 'Long-running warning snooze is CLI-only by product scope.',
  'sessions events': 'Warning acknowledgement history is CLI-only by product scope.',
  'engines add': 'Operator credential import reads private native files; never expose provider credentials through model tools.',
  'engines login': 'Hub-central provider login needs a human browser consent and a pasted one-time code; never through model tools.',
  'engines maintain': 'Operator trigger for Hub credential refresh and direct provider quota; the cron already runs it.',
  'engines list': 'Operator credential-management inventory is inspected through the CLI or dedicated account UI.',
  'engines remove': 'Operator credential withdrawal stays in the explicit credential-management CLI/UI workflow.',
  'engines sync': 'Local enrolled-host engine installation; no second remote executor.',
  'engines quota': 'Local private-account probes for the server collector; model tools read sanitized server quota instead.',
  'login': 'Host/agent pairing is performed outside model tools using hub login and explicit device approval; never return credentials in tool output.',
  'auth login': 'Use MCP OAuth/DCR/PKCE or hub auth login outside tools; no credential disclosure.',
  'auth logout': 'Use OAuth revocation or hub auth logout outside tools; revoking the current connection is transport account management.',
  'run': 'Local foreground host daemon; remote MCP must not install or start another host executor.',
  'fleet-host': 'Local host dispatcher; existing enrolled hosts own execution.',
  'subscriptions': 'Local subscription collection/reporting requires host files and host identity; safe reported subscriptions are in fleet.',
  'bridge': 'Local native mailbox bridge; existing hosts own it.',
  'bridge-resume': 'Local journal acknowledgement, no server API.',
  'workclaim-sync': 'Local native work-claim bridge; existing hosts own it.',
  'sessions host-receive': 'Local native executor, no second remote dispatcher.',
  'conversations bridge': 'Local native conversation bridge, no second remote dispatcher.',
  'task-artifact': 'Local reviewed PNG upload can exceed the bounded MCP request size; use assigned-host hub task-artifact. Download is exposed as artifact.',
  'metrics post': 'Local dashboard report projection reads host files; hosts publish it and the metrics tool reads authorized reports.',
  'update': 'Local-only CLI zipapp updater; downloads and verifies the current release for local installation.',
  'webhooks create': 'Mints a one-time route bearer secret that must never flow through model tool output or logs; operator CLI delivers it to a private 0600 file or explicit stdout.',
  'webhooks delete': 'Operator-only destructive route administration stays in the explicit CLI workflow.',
  'webhooks list': 'Route administration inventory is operator-only product scope; operators use the CLI or dashboard.',
  'webhooks show': 'Route administration detail is operator-only product scope; operators use the CLI or dashboard.',
  'webhooks deliveries': 'Delivery inspection is operator-only product scope; operators use the CLI or dashboard.',
  'webhooks test': 'Injects payloads into live sessions; stays an explicit operator action in the CLI, not a model tool.',
};
export function publicTools() {
  return routes.map(r => {
    const slots = [...r.path.matchAll(/\{(\w+)\}/g)].map(m => m[1]);
    const properties: Record<string, unknown> = Object.fromEntries(slots.map(s => [s, { type: 'string', minLength: 1, maxLength: 256 }]));
    if (r.method === 'GET' || r.name === 'sessions_cleanup') properties.query = { type: 'object', additionalProperties: { type: ['string', 'number', 'boolean'] } };
    if (r.method !== 'GET') properties.payload = { type: 'object', description: 'JSON request body.', additionalProperties: true };
    return { name: r.name, description: r.description, inputSchema: { type: 'object', properties, ...(slots.length ? { required: slots } : {}), additionalProperties: false },
      annotations: { readOnlyHint: r.method === 'GET', destructiveHint: r.method !== 'GET' && r.name !== 'sessions_capture', idempotentHint: r.method === 'GET', openWorldHint: false } };
  });
}
export function toolRequest(origin: string, name: string, args: unknown, authorization: string): Request {
  const route = routes.find(r => r.name === name);
  if (!route) throw new Error('unknown_tool');
  if (!args || typeof args !== 'object' || Array.isArray(args)) throw new Error('invalid_arguments');
  const a = args as Record<string, any>;
  const slots = [...route.path.matchAll(/\{(\w+)\}/g)].map(m => m[1]);
  if (Object.keys(a).some(k => ![...slots, 'query', ...(route.method !== 'GET' ? ['payload'] : [])].includes(k))) throw new Error('invalid_arguments');
  let path = route.path;
  for (const slot of slots) {
    const v = a[slot];
    if (typeof v !== 'string' || !v || v.length > 256 || v === '.' || v === '..' || /[\x00-\x20\x7f\\%?#]/.test(v) || (v.includes('/') && route.name !== 'claims_release')) throw new Error('invalid_path_argument');
    path = path.replace('{' + slot + '}', encodeURIComponent(v));
  }
  const url = new URL(path, origin);
  if (a.query !== undefined) {
    if (!a.query || typeof a.query !== 'object' || Array.isArray(a.query) || Object.keys(a.query).length > 30) throw new Error('invalid_query');
    for (const [k, v] of Object.entries(a.query)) {
      if (!['string', 'number', 'boolean'].includes(typeof v) || String(v).length > 4096) throw new Error('invalid_query');
      url.searchParams.set(k, String(v));
    }
  }
  if (a.payload !== undefined && (!a.payload || typeof a.payload !== 'object' || Array.isArray(a.payload))) throw new Error('invalid_payload');
  let payload = a.payload || {};
  const fixedMode = route.name === 'start_session' ? 'interactive' : ['start_agent', 'plan_agent'].includes(route.name) ? 'headless' : null;
  if (fixedMode) {
    if (payload.mode !== undefined && payload.mode !== fixedMode) throw new Error('invalid_launch_mode');
    payload = { ...payload, mode: fixedMode };
  }
  return new Request(url, { method: route.method, headers: { Authorization: authorization, 'Content-Type': 'application/json' },
    ...(route.method !== 'GET' ? { body: JSON.stringify(payload) } : {}) });
}

/** Convenience for intake updates; all reads/writes use ordinary API auth/CAS. */
export async function dispatchToolRequest(request: Request, name: string, dispatch: (r: Request) => Promise<Response>): Promise<Response> {
  if (name !== 'intake_update') return dispatch(request);
  const payload = await request.json() as Record<string, any>;
  if (payload.key === undefined) payload.key = 'update-' + crypto.randomUUID();
  const automatic = payload.expected_version === undefined;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (automatic) {
      const current = await dispatch(new Request(request.url, { headers: request.headers }));
      if (!current.ok) return current;
      const data = await current.json() as any;
      payload.expected_version = data.task.control.version;
    }
    let response: Response;
    try {
      response = await dispatch(new Request(request.url, {
        method: 'POST', headers: request.headers, body: JSON.stringify(payload),
      }));
    } catch {
      return Response.json({ error: 'intake_update_outcome_unknown',
        key: payload.key, expected_version: payload.expected_version }, { status: 502 });
    }
    if (!automatic || response.status !== 409 || attempt === 2) return response;
    const data = await response.clone().json() as any;
    if (data.error !== 'version_conflict') return response;
  }
  throw new Error('unreachable');
}
