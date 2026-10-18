/** Fixed API routes only. Every call re-enters the ordinary Hub HTTP auth path. */
type Method = 'GET' | 'POST' | 'PUT' | 'DELETE';
export interface ToolRoute { name: string; description: string; method: Method; path: string; cli: string[]; }
const routes: ToolRoute[] = [];
function tool(name: string, method: Method, path: string, description: string, cli: string[] = []) {
  routes.push({ name, method, path, description, cli });
}
tool('status', 'GET', '/api/status', 'Authorized status; query accepts since, task, project, host, after, limit.', ['status']);
tool('machines_available', 'GET', '/api/machines/available', 'List available machines.', ['machines available']);
tool('fleet', 'GET', '/api/fleet', 'Fleet machines, native sessions, agents, accounts and launch lineage.', ['fleet']);
tool('quota', 'GET', '/api/quota?view=fleet', 'Quota usage and reset evidence; query filters match hub quota.', ['quota']);
tool('auth_status', 'GET', '/api/auth/me', 'Safe human login identity (human credentials only).', ['auth status']);
tool('heartbeat', 'POST', '/api/heartbeat', 'Report own agent liveness (health:report scope).', ['heartbeat']);
for (const [name, mode] of [['start_agent', 'headless'], ['start_session', 'interactive'], ['plan_agent', 'plan']] as const) {
  tool(name, 'POST', mode === 'plan' ? '/api/fleet/plan' : '/api/fleet/launches', `Legacy fleet ${mode}: provide the same JSON as the CLI; placement/admission unchanged.`, [name.replaceAll('_', '-')]);
}
tool('sessions_list', 'GET', '/api/sessions', 'List sessions; query view=lifecycle gives grouped lifecycle; state, limit, cursor filter.', ['sessions', 'sessions list']);
tool('sessions_tree', 'GET', '/api/sessions/tree', 'Session requester/parent tree.', ['sessions tree']);
tool('sessions_start', 'POST', '/api/sessions', 'Queue a session. payload: workspace, engine, machine, mode (headless/interactive), brief, name, parent_id, key. Admission and quotas remain enforced.', ['sessions start']);
tool('sessions_show', 'GET', '/api/sessions/{id}', 'Session binding and observed state.', ['sessions show']);
tool('sessions_commands', 'GET', '/api/sessions/{id}/commands', 'Read command results; recorded/transported is not acknowledged execution.', ['sessions commands']);
for (const action of ['send', 'capture', 'stop']) tool(`sessions_${action}`, 'POST', `/api/sessions/{id}/${action}`,
  action === 'capture' ? 'Queue a sanitized capture; payload: key, max_bytes, screen. Poll sessions_commands for the result.' :
  `DESTRUCTIVE: queue ${action}; payload: key${action === 'send' ? ', body' : ''}. Never infer completion from enqueue; poll sessions_commands.`, [`sessions ${action}`]);
tool('sessions_cancel', 'POST', '/api/sessions/{id}/cancel', 'Cancel only an owned held intent before any Fleet/native start. payload: key. Reuse the stable key; the no_native_start receipt is cancellation, not confirmed native stop.', ['sessions cancel']);
tool('sessions_protect', 'POST', '/api/sessions/{id}/protect', 'Permanently protect an owned session from automated cleanup. payload: key. No unprotect or native effect. Exact current owner credential family is required.', ['sessions protect']);
tool('sessions_cleanup', 'POST', '/api/sessions/cleanup', 'DESTRUCTIVE when dry_run=false: plan/apply cleanup through existing custody guards. Explicit payload dry_run=true for a preview.', ['sessions cleanup']);
tool('sessions_observe', 'POST', '/api/sessions/observations', 'Host-only lifecycle observation; ownership remains server validated.', ['sessions observe']);
for (const action of ['send', 'reply']) tool(`bus_${action}`, 'POST', `/api/bus/${action}`, 'DESTRUCTIVE: send a durable message using payload from_actor, to_actor, kind, body, idempotency_key and optional thread_id, reply_to, lease_epoch.', [action]);
tool('bus_inbox', 'GET', '/api/bus/inbox', 'Inbox query: actor, limit, cursor.', ['inbox']);
tool('bus_show', 'GET', '/api/bus/messages/{id}', 'Read a message.', ['show']);
for (const stage of ['ack', 'accept', 'complete']) tool(`bus_${stage}`, 'POST', `/api/bus/messages/{id}/${stage}`, `Explicitly ${stage} a message; optional payload lease_epoch.`, [stage]);
tool('claims_list', 'GET', '/api/bus/claims', 'Claims query: project, holder, all.', ['claims']);
tool('claims_create', 'POST', '/api/bus/claims', 'Claim a scope using project, scope, holder, ttl_ms, lease_epoch.', ['claim']);
tool('claims_release', 'POST', '/api/bus/claims/{id}/release', 'Release a claim; id may be project:scope (encoded as one segment).', ['release']);
tool('tasks_submit', 'POST', '/api/fleet/tasks', 'Submit authorized work; payload prompt, approved_action, needs, workspace, idempotency_key and routing preferences, or tasks batch.', ['submit']);
tool('routing_machines', 'GET', '/api/fleet/routing/machines', 'Capability and model advertisements.', ['routing-machines']);
tool('model_policy', 'GET', '/api/fleet/routing/policy', 'Configured model policy.', ['model-policy']);
tool('jobs_list', 'GET', '/api/fleet/tasks', 'Recent routed tasks.', ['jobs']);
tool('jobs_show', 'GET', '/api/fleet/tasks/{id}', 'Task state, report and artifacts.', ['job']);
tool('task_result', 'POST', '/api/fleet/tasks/{id}/result', 'Assigned host only: status/report/artifacts/confirmed_exit.', ['task-result']);
tool('artifact', 'GET', '/api/fleet/tasks/{id}/artifacts/{name}', 'Fetch a sanitized PNG as the API base64 DTO.', ['artifact']);
tool('intake_create', 'POST', '/api/intake/tasks', 'File verbatim task: payload key, project, title, text, transcription, source {type,reference,timestamp}.', ['intake create']);
tool('intake_list', 'GET', '/api/intake/tasks', 'One revision-pinned page: query status, project, after, revision, authorization_valid_until, limit.', ['intake list']);
for (const action of ['show', 'history', 'links', 'context']) tool(`intake_${action}`, 'GET', `/api/intake/tasks/{id}${action === 'show' ? '' : '/' + action}`, `Read task ${action}.`, [`intake ${action}`]);
for (const action of ['continue', 'update', 'append', 'assign']) tool(`intake_${action}`, 'POST', `/api/intake/tasks/{id}${action === 'update' ? '' : '/'}${action === 'append' ? 'events' : action === 'update' ? '' : action}`, `Task ${action}: same payload as CLI, including key and expected_version. Assign queues a real Hub session.`, [`intake ${action}`]);
for (const group of ['rules', 'grants']) {
  tool(`intake_${group}`, 'GET', `/api/intake/${group}`, `List operator ${group}.`, [`intake ${group}`]);
  tool(`intake_${group}_write`, 'POST', `/api/intake/${group}`, `Operator-only ${group} mutation; payload matches web API.`);
}
tool('intake_revoke', 'POST', '/api/intake/grants/{id}/revoke', 'Revoke a grant, preserving history.', ['intake revoke']);
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
  'login': 'Host/agent pairing is performed outside model tools using hub login and explicit device approval; never return credentials in tool output.',
  'auth login': 'Use MCP OAuth/DCR/PKCE or hub auth login outside tools; no credential disclosure.',
  'auth logout': 'Use OAuth revocation or hub auth logout outside tools; revoking the current connection is transport account management.',
  'run': 'Local foreground host daemon; remote MCP must not install or start another host executor.',
  'fleet-host': 'Local host dispatcher; existing enrolled hosts own execution.',
  'subscriptions': 'Local subscription collection/reporting requires host files and host identity; safe reported subscriptions are in fleet.',
  'bridge': 'Local native mailbox bridge; existing hosts own it.',
  'bridge-resume': 'Local journal acknowledgement, no server API.',
  'sessions host-receive': 'Local native executor, no second remote dispatcher.',
  'conversations bridge': 'Local native conversation bridge, no second remote dispatcher.',
  'task-artifact': 'Local reviewed PNG upload can exceed the bounded MCP request size; use assigned-host hub task-artifact. Download is exposed as artifact.',
  'metrics post': 'Local dashboard report projection reads host files; hosts publish it and the metrics tool reads authorized reports.',
  'update': 'Local-only CLI zipapp updater; downloads and verifies the current release for local installation.',
};
export function publicTools() {
  return routes.map(r => {
    const slots = [...r.path.matchAll(/\{(\w+)\}/g)].map(m => m[1]);
    const properties: Record<string, unknown> = Object.fromEntries(slots.map(s => [s, { type: 'string', minLength: 1, maxLength: 256 }]));
    properties.query = { type: 'object', description: 'API query parameters; scalar values only.', additionalProperties: { type: ['string', 'number', 'boolean'] } };
    if (r.method !== 'GET') properties.payload = { type: 'object', description: r.description, additionalProperties: true };
    return { name: r.name, description: r.description, inputSchema: { type: 'object', properties, required: slots, additionalProperties: false },
      annotations: { title: r.name, readOnlyHint: r.method === 'GET', destructiveHint: r.method !== 'GET', idempotentHint: r.method === 'GET', openWorldHint: false } };
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
