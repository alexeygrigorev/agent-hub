import { CLI_EXCLUSIONS as BASE_CLI_EXCLUSIONS, MCP_ROUTES, publicTools, type ToolRoute } from './mcp-tools';

/** The original 87-tool MCP surface, retained for the explicit legacy toolset. */
export const LEGACY_MCP_TOOL_NAMES = [
  'status', 'machines_available', 'fleet', 'quota', 'auth_status', 'heartbeat',
  'start_agent', 'start_session', 'plan_agent', 'sessions_list', 'sessions_tree',
  'sessions_start', 'sessions_show', 'sessions_commands', 'sessions_send',
  'sessions_capture', 'sessions_stop', 'sessions_cancel', 'sessions_protect',
  'sessions_cleanup', 'sessions_observe', 'bus_send', 'bus_reply', 'bus_inbox',
  'bus_show', 'bus_ack', 'bus_accept', 'bus_complete', 'claims_list',
  'bus_activity', 'claims_create', 'claims_release', 'tasks_submit',
  'routing_machines', 'model_policy', 'jobs_list', 'jobs_show', 'task_result',
  'artifact', 'intake_create', 'intake_list', 'intake_show', 'intake_history',
  'intake_links', 'intake_context', 'intake_continue', 'intake_update',
  'intake_append', 'intake_assign', 'intake_rules', 'intake_rules_write',
  'intake_grants', 'intake_grants_write', 'intake_revoke', 'intake_grantees',
  'handoff_page', 'conversations_list', 'conversations_participants',
  'conversations_get', 'conversations_members', 'conversations_history',
  'conversations_create', 'conversations_send', 'conversations_read',
  'conversations_members_add', 'conversations_members_remove', 'agents',
  'liveness', 'checks', 'metrics', 'checks_write', 'checks_run',
  'conversations_archive', 'conversations_unarchive',
  'conversations_links_search', 'conversations_links',
  'conversations_follow_through', 'conversations_link_show',
  'conversations_link_create', 'conversations_link_remove',
  'conversations_automation', 'conversations_automation_remove',
  'fleet_claim_policy', 'fleet_claim_policy_write', 'monitoring_disk',
  'monitoring_health', 'accounts_write',
] as const;

export const CORE_TOOL_NAMES = [
  'intake_create', 'intake_list', 'intake_show', 'intake_update',
  'intake_append', 'intake_assign', 'handoff_page', 'sessions_list',
  'sessions_show', 'sessions_commands', 'sessions_start', 'sessions_send',
  'sessions_capture', 'sessions_stop', 'sessions_cancel', 'sessions_protect',
  'sessions_cleanup', 'status',
] as const;

export const DOMAIN_TOOL_NAMES = [
  'intake_governance', 'bus_read', 'bus_write', 'conversations_read',
  'conversations_write', 'fleet_read', 'ops_write',
] as const;

/** Host callbacks and replaced launch/job calls stay callable by name but hidden from discovery. */
export const DEFAULT_TOOL_EXCLUSIONS = [
  'heartbeat', 'sessions_observe', 'task_result', 'start_agent', 'start_session',
  'plan_agent', 'tasks_submit', 'jobs_list', 'jobs_show', 'artifact',
] as const;

const PHASE2_CLI_EXCLUSIONS: Record<string, string> = {
  heartbeat: 'Host-only daemon callback; authenticated host agents report liveness directly.',
  'sessions observe': 'Host-only lifecycle callback; authenticated host agents report observations directly.',
  'task-result': 'Assigned-host callback; remote clients cannot submit host execution receipts.',
  'start-agent': 'Superseded fleet launch alias; use sessions_start for queued Hub sessions.',
  'start-session': 'Superseded interactive launch alias; use sessions_start for queued Hub sessions.',
  'plan-agent': 'Superseded launch planner; use Hub placement policy through sessions_start.',
  submit: 'Superseded task launcher; work enters through intake assignment and sessions_start.',
  jobs: 'Superseded routed-job listing; inspect managed Hub sessions instead.',
  job: 'Superseded routed-job detail; inspect managed Hub sessions instead.',
  artifact: 'Superseded job artifact fetch; use assigned-task evidence APIs.',
};

/** Combined CLI-only inventory, including routes hidden from the curated MCP list. */
export const CLI_EXCLUSIONS: Readonly<Record<string, string>> = Object.freeze({
  ...BASE_CLI_EXCLUSIONS,
  ...PHASE2_CLI_EXCLUSIONS,
});

const core = new Set<string>(CORE_TOOL_NAMES);
const domains = new Set<string>(DOMAIN_TOOL_NAMES);
const excludedFromDefault = new Set<string>(DEFAULT_TOOL_EXCLUSIONS);
const coreAliases: Record<string, string> = {
  intake_history: 'intake_show',
  intake_links: 'intake_show',
  intake_context: 'intake_show',
  sessions_tree: 'sessions_list',
};
const byName = new Map(MCP_ROUTES.map(route => [route.name, route]));

function domainForRoute(route: ToolRoute): string {
  if (core.has(route.name)) return route.name;
  if (coreAliases[route.name]) return coreAliases[route.name];
  if (route.name.startsWith('bus_') || route.name.startsWith('msg_') || route.name.startsWith('claims_'))
    return route.method === 'GET' ? 'bus_read' : 'bus_write';
  // The historical conversations_read route is POST /read and records a
  // receipt, so keep it in the write domain despite its legacy name.
  if (route.name === 'conversations_read') return 'conversations_write';
  if (route.name.startsWith('conversations_'))
    return route.method === 'GET' ? 'conversations_read' : 'conversations_write';
  if (route.name.startsWith('intake_') || route.name.startsWith('worktree_'))
    return route.method === 'GET' ? 'intake_governance' : 'ops_write';
  return route.method === 'GET' ? 'fleet_read' : 'ops_write';
}

function canonicalForRoute(route: ToolRoute): string {
  return domainForRoute(route);
}

const routeAliases = Object.fromEntries(MCP_ROUTES.map(route => [route.name, canonicalForRoute(route)]));
const legacyNameSet = new Set<string>(LEGACY_MCP_TOOL_NAMES);
const missingLegacy = LEGACY_MCP_TOOL_NAMES.filter(name => !byName.has(name));
if (missingLegacy.length) throw new Error(`legacy_mcp_route_missing:${missingLegacy.join(',')}`);
if (LEGACY_MCP_TOOL_NAMES.length !== 87 || new Set(LEGACY_MCP_TOOL_NAMES).size !== 87)
  throw new Error('legacy_mcp_surface_must_contain_87_unique_tools');

/** Stable old-name to canonical-name index used for discovery, dispatch and telemetry. */
export const LEGACY_MCP_ALIASES: Readonly<Record<string, string>> = Object.freeze(
  Object.fromEntries(LEGACY_MCP_TOOL_NAMES.map(name => [name, routeAliases[name]])),
);

/** Calls added after the 87-tool baseline remain accepted while the legacy list stays stable. */
export const EXTENDED_MCP_ALIASES: Readonly<Record<string, string>> = Object.freeze(
  Object.fromEntries(MCP_ROUTES.filter(route => !legacyNameSet.has(route.name)).map(route => [route.name, routeAliases[route.name]])),
);

const actions: Record<string, Record<string, ToolRoute>> = Object.fromEntries(
  DOMAIN_TOOL_NAMES.map(name => [name, {}]),
);
for (const route of MCP_ROUTES) {
  const domain = domainForRoute(route);
  if (!domains.has(domain) || coreAliases[route.name] || core.has(route.name) || excludedFromDefault.has(route.name)) continue;
  // The required canonical name conversations_read overlaps the legacy leaf.
  // Give its POST read-receipt operation a distinct enum value.
  actions[domain][route.name === 'conversations_read' ? 'mark_read' : route.name] = route;
}

function domainAnnotations(name: string) {
  const readOnly = ['intake_governance', 'bus_read', 'conversations_read', 'fleet_read'].includes(name);
  return readOnly
    ? { readOnlyHint: true, destructiveHint: false, openWorldHint: false }
    : { readOnlyHint: false, destructiveHint: true, openWorldHint: false };
}

function domainTool(name: string) {
  const routes = Object.values(actions[name]);
  const slots = new Set(routes.flatMap(route => [...route.path.matchAll(/\{(\w+)\}/g)].map(match => match[1])));
  const properties: Record<string, unknown> = {
    action: { type: 'string', enum: Object.keys(actions[name]) },
  };
  for (const slot of slots) {
    const property = slot === 'action' ? 'subaction' : slot;
    properties[property] = { type: 'string', minLength: 1, maxLength: 256 };
  }
  if (routes.some(route => route.method === 'GET' || route.name === 'sessions_cleanup'))
    properties.query = { type: 'object', additionalProperties: { type: ['string', 'number', 'boolean'] } };
  if (routes.some(route => route.method !== 'GET'))
    properties.payload = { type: 'object', description: 'JSON request body.' };
  return {
    name,
    description: `Hub ${name.replace('_', ' ')} operations. Choose action; route arguments use the existing Hub API contract.`,
    inputSchema: { type: 'object', properties, required: ['action'], additionalProperties: false },
    annotations: domainAnnotations(name),
  };
}

/** Default curated 25-tool discovery surface. */
export function curatedTools() {
  const full = new Map(publicTools().map(tool => [tool.name, tool]));
  const result: any[] = CORE_TOOL_NAMES.map(name => {
    const tool = full.get(name);
    if (!tool) throw new Error(`core_mcp_route_missing:${name}`);
    return structuredClone(tool);
  });
  const intakeShow = result.find(tool => tool.name === 'intake_show');
  intakeShow.inputSchema.properties.query = {
    type: 'object', properties: { view: { type: 'string', enum: ['history', 'links', 'context'] } },
    additionalProperties: { type: ['string', 'number', 'boolean'] },
  };
  const sessionsList = result.find(tool => tool.name === 'sessions_list');
  sessionsList.inputSchema.properties.query = {
    type: 'object', properties: { view: { type: 'string', enum: ['lifecycle', 'tree'] } },
    additionalProperties: { type: ['string', 'number', 'boolean'] },
  };
  result.push(...DOMAIN_TOOL_NAMES.map(domainTool));
  if (result.length !== 25) throw new Error('curated_mcp_surface_must_contain_25_tools');
  return result;
}

/** Full historical 87-tool schema set for clients that opt into legacy discovery. */
export function legacyTools() {
  const full = new Map(publicTools().map(tool => [tool.name, tool]));
  return LEGACY_MCP_TOOL_NAMES.map(name => {
    const tool = full.get(name);
    if (!tool) throw new Error(`legacy_mcp_tool_missing:${name}`);
    return tool;
  });
}

export interface ResolvedMcpCall {
  routeName: string;
  args: unknown;
  canonicalName: string;
  usageKind: 'alias' | 'canonical';
}

/** Resolve the compact action tools and old names to the unchanged HTTP route adapter. */
export function resolveMcpCall(name: string, rawArgs: unknown): ResolvedMcpCall {
  if (domains.has(name)) {
    if (!rawArgs || typeof rawArgs !== 'object' || Array.isArray(rawArgs)) throw new Error('invalid_arguments');
    const args = rawArgs as Record<string, unknown>;
    // Keep the old conversations_read argument shape callable alongside the
    // canonical action-enum form with the same MCP tool name.
    if (name === 'conversations_read' && typeof args.action !== 'string') {
      return { routeName: name, args: rawArgs, canonicalName: routeAliases[name], usageKind: 'alias' };
    }
    if (typeof args.action !== 'string') throw new Error('invalid_action');
    const route = actions[name][args.action];
    if (!route) throw new Error('invalid_action');
    const { action: _selectedAction, subaction, ...routeArgs } = args;
    const slots = [...route.path.matchAll(/\{(\w+)\}/g)].map(match => match[1]);
    if (slots.includes('action')) (routeArgs as Record<string, unknown>).action = subaction;
    return { routeName: route.name, args: routeArgs, canonicalName: name, usageKind: 'canonical' };
  }
  const route = byName.get(name);
  if (!route) throw new Error('unknown_tool');
  const args = normalizeCoreView(name, rawArgs);
  const canonicalName = routeAliases[name];
  return {
    routeName: args.routeName,
    args: args.args,
    canonicalName,
    usageKind: name === canonicalName ? 'canonical' : 'alias',
  };
}

export function canonicalNameForTool(name: string): string {
  if (domains.has(name)) return name;
  return routeAliases[name] || 'unknown';
}

function normalizeCoreView(name: string, rawArgs: unknown): { routeName: string; args: unknown } {
  if (!rawArgs || typeof rawArgs !== 'object' || Array.isArray(rawArgs)) return { routeName: name, args: rawArgs };
  const args = rawArgs as Record<string, unknown>;
  const query = args.query;
  if (!query || typeof query !== 'object' || Array.isArray(query)) return { routeName: name, args: rawArgs };
  const values = query as Record<string, unknown>;
  if (name === 'intake_show' && values.view !== undefined) {
    if (!['history', 'links', 'context'].includes(String(values.view))) throw new Error('invalid_view');
    const routeName = `intake_${String(values.view)}`;
    const rest = { ...values };
    delete rest.view;
    const nextArgs = { ...args, ...(Object.keys(rest).length ? { query: rest } : {}) };
    if (!Object.keys(rest).length) delete (nextArgs as any).query;
    return { routeName, args: nextArgs };
  }
  if (name === 'sessions_list' && values.view !== undefined) {
    if (!['lifecycle', 'tree'].includes(String(values.view))) throw new Error('invalid_view');
    const rest = { ...values };
    delete rest.view;
    const nextArgs = { ...args, ...(Object.keys(rest).length ? { query: rest } : {}) };
    if (!Object.keys(rest).length) delete (nextArgs as any).query;
    return { routeName: values.view === 'tree' ? 'sessions_tree' : name, args: nextArgs };
  }
  return { routeName: name, args: rawArgs };
}

export function legacyRouteNames() {
  return LEGACY_MCP_TOOL_NAMES;
}
