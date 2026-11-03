import { describe, expect, it } from 'vitest';
import { handleUiSearch, parseUiSearchInput, searchUiRecords } from '../src/ui-search';

class SearchSql {
  sessions: Record<string, any>[] = [];
  tasks: Record<string, any>[] = [];
  machines: Record<string, any>[] = [];
  exportDone = 1;
  exec(query: string, ...args: any[]) {
    if (query.includes('FROM intake_export_migration')) return { toArray: () => [{ done: this.exportDone }] };
    if (query.includes("sqlite_master") && query.includes("intake_export")) return { toArray: () => [{ name: 'intake_export' }] };
    if (query.includes('FROM session_control_sessions')) {
      const after = String(args[0] ?? '');
      const limit = Number(args.at(-1));
      return { toArray: () => this.sessions.filter(row => row.id > after).sort((a,b) => a.id.localeCompare(b.id)).slice(0, limit) };
    }
    if (query.includes('FROM intake_export')) {
      const after = String(args[0] ?? '');
      const limit = Number(args.at(-1));
      return { toArray: () => this.tasks.filter(row => row.id > after).sort((a,b) => a.id.localeCompare(b.id)).slice(0, limit) };
    }
    if (query.includes('FROM machines')) {
      const after = String(args[0] ?? '');
      const limit = Number(args.at(-1));
      return { toArray: () => this.machines.filter(row => row.id > after && row.revoked_at == null).sort((a,b) => a.id.localeCompare(b.id)).slice(0, limit) };
    }
    throw new Error(`unexpected SQL: ${query}`);
  }
}

function url(params: Record<string, string> = {}) {
  const search = new URLSearchParams({ q: 'needle', type: 'sessions', ...params });
  return new URL(`https://hub.test/api/ui-search?${search}`);
}

describe('UI global search contract', () => {
  it('rejects missing, oversized and control-character queries and excessive limits', () => {
    expect(parseUiSearchInput(new URL('https://hub.test/api/ui-search?q=x&type=sessions'))).toEqual({ error: 'invalid_query' });
    expect(parseUiSearchInput(url({ q: 'a'.repeat(101) }))).toEqual({ error: 'invalid_query' });
    expect(parseUiSearchInput(url({ q: 'ok\nsecret' }))).toEqual({ error: 'invalid_query' });
    expect(parseUiSearchInput(url({ limit: '21' }))).toEqual({ error: 'invalid_limit' });
  });

  it('finds an older record through bounded scan cursors and matches IDs with a friendly title', () => {
    const sql = new SearchSql();
    for (let n = 0; n < 120; n++) {
      const id = `session-${String(n).padStart(3, '0')}`;
      sql.sessions.push({ id, actor: 'human:operator', state: 'running', machine_id: 'host-a', engine: 'codex', created_at: '2026-11-01',
        request_json: JSON.stringify({ name: n === 113 ? 'Friendly title' : `Routine task ${n}`, brief: 'private prompt needle should never leave the server' }),
        binding_json: JSON.stringify({ native_id: 'native-needle-id', tag: 'alias-needle' }) });
    }
    const input = parseUiSearchInput(url({ q: 'session-113' }));
    expect('error' in input).toBe(false);
    if ('error' in input) return;
    const first = searchUiRecords(sql as any, input);
    expect(first.scanned).toBe(100);
    expect(first.results).toHaveLength(0);
    expect(first.partial).toBe(true);
    expect(first.next_cursor).toBeTruthy();

    const nextInput = parseUiSearchInput(url({ q: 'session-113', cursor: first.next_cursor! }));
    expect('error' in nextInput).toBe(false);
    if ('error' in nextInput) return;
    const second = searchUiRecords(sql as any, nextInput);
    expect(second.results.map(result => result.title)).toEqual(['Friendly title']);
    expect(second.results[0].id).toBe('session-113');
    expect(second.partial).toBe(false);
    expect(JSON.stringify(second)).not.toContain('private prompt');
  });

  it('matches an alias even when a human-readable title is present', () => {
    const sql = new SearchSql();
    sql.sessions.push({ id: 'uuid-12345678', state: 'running', machine_id: 'host-a', engine: 'codex', created_at: 'now',
      request_json: JSON.stringify({ name: 'Friendly session' }), binding_json: JSON.stringify({ tag: 'my-alias' }) });
    const parsed = parseUiSearchInput(url({ q: 'my-alias' }));
    if ('error' in parsed) throw new Error(parsed.error);
    expect(searchUiRecords(sql as any, parsed).results[0].title).toBe('Friendly session');
    const titleSearch = parseUiSearchInput(url({ q: 'friendly session' }));
    if ('error' in titleSearch) throw new Error(titleSearch.error);
    expect(searchUiRecords(sql as any, titleSearch).results[0].id).toBe('uuid-12345678');
  });

  it('searches indexed task IDs/project and machine ID/name fields without returning private columns', () => {
    const sql = new SearchSql();
    sql.tasks.push({ id: 'intake-older-0042', project: 'agent-hub', status: 'blocked', archived: 0,
      data: JSON.stringify({ id: 'intake-older-0042', title: 'Friendly task', owner: 'owner-private', notes: 'private note' }) });
    sql.machines.push({ id: 'machine-older-0088', name: 'Friendly host', hostname: 'host-alias', drained: 0,
      last_checkin_at: new Date().toISOString(), revoked_at: null, capabilities: 'private-capability' });

    const taskInput = parseUiSearchInput(url({ q: 'older-0042', type: 'tasks', scope: 'all' }));
    if ('error' in taskInput) throw new Error(taskInput.error);
    const task = searchUiRecords(sql as any, taskInput).results[0];
    expect(task).toMatchObject({ id: 'intake-older-0042', title: 'Friendly task', status: 'blocked', context: 'agent-hub', href: '/intake/intake-older-0042' });
    expect(JSON.stringify(task)).not.toContain('private note');
    expect(JSON.stringify(task)).not.toContain('owner-private');
    const projectTask = parseUiSearchInput(url({ q: 'Friendly task', type: 'tasks', scope: 'all', project: 'agent-hub' }));
    if ('error' in projectTask) throw new Error(projectTask.error);
    expect(searchUiRecords(sql as any, projectTask).results[0].id).toBe('intake-older-0042');

    const machineInput = parseUiSearchInput(url({ q: 'host-alias', type: 'machines' }));
    if ('error' in machineInput) throw new Error(machineInput.error);
    const machine = searchUiRecords(sql as any, machineInput).results[0];
    expect(machine).toMatchObject({ id: 'machine-older-0088', title: 'Friendly host', context: 'host-alias', href: '/machines.html?machine=machine-older-0088' });
    expect(JSON.stringify(machine)).not.toContain('private-capability');
    const friendlyMachine = parseUiSearchInput(url({ q: 'Friendly host', type: 'machines', scope: 'all' }));
    if ('error' in friendlyMachine) throw new Error(friendlyMachine.error);
    expect(searchUiRecords(sql as any, friendlyMachine).results[0].id).toBe('machine-older-0088');
  });

  it('binds scan cursors to the query and facets', () => {
    const sql = new SearchSql();
    sql.sessions = Array.from({ length: 101 }, (_, index) => ({ id: `s-${String(index).padStart(3, '0')}`, state: 'running',
      request_json: JSON.stringify({ name: `work ${index}` }), binding_json: '{}', created_at: 'now' }));
    const input = parseUiSearchInput(url({ q: 'absent' }));
    if ('error' in input) throw new Error(input.error);
    const page = searchUiRecords(sql as any, input);
    const mismatched = parseUiSearchInput(url({ q: 'different', cursor: page.next_cursor! }));
    expect(mismatched).toEqual({ error: 'invalid_cursor' });
  });

  it('requires operator authorization and GET', async () => {
    const sql = new SearchSql();
    expect(handleUiSearch(new Request(url()), sql as any, false).status).toBe(403);
    expect(handleUiSearch(new Request(url(), { method: 'POST' }), sql as any, true).status).toBe(405);
    const response = handleUiSearch(new Request(url()), sql as any, true);
    expect(response.status).toBe(200);
    expect(response.headers.get('Cache-Control')).toBe('private, no-store');
  });

  it('does not claim complete search while the bounded intake projection is still indexing', async () => {
    const sql = new SearchSql(); sql.exportDone = 0;
    const response = handleUiSearch(new Request(url()), sql as any, true);
    expect(response.status).toBe(503);
    expect(await response.json()).toMatchObject({ error: 'search_indexing', partial: true, retry_after: 1 });
  });
});
