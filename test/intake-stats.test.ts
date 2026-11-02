import { describe, expect, it } from 'vitest';
import { aggregateIntakeStats, projectIntakeStats, projectVisibleIntakeStats, indexStatsSources, statsTime } from '../src/intake-stats';
const now = Date.parse('2026-10-28T12:00:00Z');
const at = (n: number) => now - n;
const task = (history: any[] = []) => ({ id: 'task', title: 'Fixture', project: 'demo', status: 'open', created_at: at(10000), history });
const empty = () => ({ sessions: [] as any[], launches: [] as any[], natives: [] as any[], dependencies: [] as any[] });
describe('intake statistics backfill', () => {
  it('does not turn queue time, observation time or updated_at into native runtime', () => {
    const sources = empty(); sources.sessions.push({ id: 'q', request_json: '{"intake_task_id":"task"}', state: 'cancelled', created_at: new Date(at(8000)).toISOString(), updated_at: new Date(at(2000)).toISOString() });
    const result = projectIntakeStats(task(), sources, now);
    expect(result).toMatchObject({ attempt_count: 1, session_count: 0, stopped_attempts: 1, first_session_started_at: null, done_at: null, total_session_wall_time_ms: null });
    expect(result.unknown_reasons.total_session_wall_time_ms).toBeTruthy();
  });
  it('deduplicates canonical and linked fallback native identities and uses retained terminal evidence', () => {
    const sources = empty();
    sources.sessions.push({ id: 's', machine_id: 'm', engine: 'codex', state: 'stopped', request_json: '{}', binding_json: JSON.stringify({native_id: 'native', native_created_at_ms: at(8000), retirement: {finished_at_ms: at(3000)}}) });
    sources.natives.push({ machine_id: 'm', native_id: 'native', created_at_ms: at(8000), account: 'account-a' });
    const result = projectIntakeStats(task([{kind: 'session', id: 'link', native_id: 'native', host: 'm'}, {kind: 'link', id: 'duplicate', hub_session_id: 's'}, {kind: 'note', text: 'DONE\nLive check: fixture', captured_at: at(1000)}]), sources, now);
    expect(result).toMatchObject({ attempt_count: 1, session_count: 1, first_session_started_at: at(8000), done_at: at(1000), lead_time_ms: 9000, cycle_time_ms: 7000, total_session_wall_time_ms: 5000, total_session_active_time_ms: null, accounts: ['account-a'] });
  });
  it('keeps distinct native incarnations separate', () => {
    const sources = empty();
    sources.sessions = [8000, 4000].map((age,i) => ({id:'s'+i,machine_id:'m',state:'stopped',request_json:'{"intake_task_id":"task"}',binding_json:JSON.stringify({native_id:'same',native_created_at_ms:at(age),retirement:{finished_at_ms:at(age-1000)}})}));
    expect(projectIntakeStats(task(),sources,now)).toMatchObject({attempt_count:2,session_count:2,total_session_wall_time_ms:2000});
  });
  it('includes linked external fallback without a Hub start row, never matches ambiguous hosts', () => {
    const sources = empty(); sources.natives.push({machine_id:'a',native_id:'n',created_at_ms:at(7000)}, {machine_id:'b',native_id:'n',created_at_ms:at(9000)});
    const result = projectIntakeStats(task([{kind:'link',id:'l',native_id:'n'}]), sources, now);
    expect(result.first_session_started_at).toBeNull();
    expect(projectIntakeStats(task([{kind:'link',id:'l',native_id:'n',host:'a'}]), sources, now).first_session_started_at).toBe(at(7000));
    const fallback = projectIntakeStats(task([{kind:'link',id:'l',native_id:'n',host:'a'}]),sources,now);
    expect(aggregateIntakeStats([fallback],'24h',now).top_intakes_by_attempts).toEqual([{id:'task',title:'Fixture',attempt_count:1}]);
  });
  it('keeps retained assignment links when canonical session rows have gone', () => {
    const result = projectIntakeStats(task([{kind:'link',id:'l',hub_session_id:'lost'}, {kind:'link',id:'dup',hub_session_id:'lost'}]),empty(),now);
    expect(result).toMatchObject({attempt_count:1,first_session_started_at:null,total_session_wall_time_ms:null});
    expect(result.attempts[0].id).toBe('lost');
  });
  it('does not present link hints or missing outcomes as proven starts or zero totals', () => {
    for (const hint of [{native_id:'hint',host:'m'}, {hub_session_id:'lost'}]) {
      const result = projectIntakeStats(task([{kind:'link',id:'l',...hint}]),empty(),now);
      expect(result).toMatchObject({attempt_count:1,session_count:null,failed_attempts:null,stopped_attempts:null,restarted_attempts:null,
        known_counts:{sessions:0,failed:0,stopped:0,restarted:0},unknown_reasons:{session_count:'one_or_more_linked_starts_unproven',failed_attempts:'one_or_more_attempt_outcomes_unknown'}});
    }
    const sources=empty();sources.natives.push({machine_id:'m',native_id:'real',created_at_ms:at(5000),phase:'exited'});
    const mixed=projectIntakeStats(task([{kind:'link',id:'known',host:'m',native_id:'real'},{kind:'link',id:'lost',hub_session_id:'lost'}]),sources,now);
    expect(mixed).toMatchObject({session_count:null,first_session_started_at:null,known_counts:{sessions:1}});
  });
  it('labels observed Hub terminal time separately from native retirement time', () => {
    const sources=empty();sources.launches.push({id:'launch',request_json:'{"intake_task_id":"task"}',machine_id:'m',native_id:'n',state:'failed',finished_at:at(1000)});
    sources.natives.push({machine_id:'m',native_id:'n',created_at_ms:at(5000)});
    expect(projectIntakeStats(task(),sources,now).attempts[0]).toMatchObject({wall_time_ms:4000,finished_at_basis:'hub_terminal_observation',wall_time_basis:'hub_terminal_observation'});
  });
  it('uses a status-done audit event and counts blockers and dependency edges', () => {
    const sources = empty(); sources.dependencies.push({task_id:'task',dependency_id:'other'});
    const result = projectIntakeStats(task([{id:'b',kind:'blocker'}, {kind:'update',changes:{status:'done'},captured_at:at(1000)}]), sources, now);
    expect(result).toMatchObject({done_at:at(1000),lead_time_ms:9000,blocker_count:2,blocker_event_count:1,blocker_link_count:1});
  });
  it('retains unknown DONE time rather than substituting a later edit timestamp', () => {
    expect(projectIntakeStats({...task(),status:'done',updated_at:now},empty(),now)).toMatchObject({done_at:null,unknown_reasons:{done_at:'done_timestamp_not_retained'}});
    expect(statsTime('2026-10-28T12:00:00')).toBeNull();
  });
  it('calculates medians and DONE cohort attempts while ranking recent attempt creation', () => {
    const items = [10, 20, 30, 40].map((lead, i) => ({id:String(i),title:String(i),done_at:at(1000),lead_time_ms:lead,attempt_count:i+1,attempts:Array.from({length:i+1},()=>({created_at:at(1000)}))}));
    const a = aggregateIntakeStats(items,'24h',now);
    expect(a).toMatchObject({done_count:4,median_lead_time_ms:25,attempts_per_done:2.5});
    expect(a.top_intakes_by_attempts[0]).toMatchObject({id:'3',attempt_count:4});
    expect(aggregateIntakeStats(items.map(i=>({...i,done_at:at(2*86400000)})),'24h',now).attempts_per_done).toBeNull();
  });
  it('keeps undated DONE records in all-time cohorts and exposes incomplete window coverage', () => {
    const legacy=projectIntakeStats({...task([{kind:'link',id:'a',hub_session_id:'lost-a'},{kind:'link',id:'b',hub_session_id:'lost-b'}]),status:'done'},empty(),now);
    expect(aggregateIntakeStats([legacy],'all',now)).toMatchObject({done_count:1,attempts_per_done:2,median_lead_time_ms:null,undated_done_count:1,undated_attempt_count:2,top_ranking_complete:true});
    const dated={...legacy,id:'dated',done_at:at(1000),lead_time_ms:100,attempt_count:1,attempts:[{created_at:at(2000)}]};
    for (const window of ['24h','7d','30d'] as const) {
      expect(aggregateIntakeStats([legacy,dated],window,now)).toMatchObject({done_count:null,dated_done_count:1,attempts_per_done:null,median_lead_time_ms:100,measured_lead_count:1,top_ranking_complete:false,
        unknown_reasons:{done_count:'undated_done_intakes_cannot_be_assigned_to_window',top_intakes_by_attempts:'partial_ranking_undated_attempts_excluded'}});
    }
    expect(aggregateIntakeStats([legacy],'24h',now).unknown_reasons.attempts_per_done).not.toBe('no_done_intakes_in_window');
    const undatedNote=projectIntakeStats(task([{kind:'note',text:'DONE'}]),empty(),now);
    expect(aggregateIntakeStats([undatedNote],'all',now)).toMatchObject({done_count:1,undated_done_count:1});
  });
});

import { env, SELF, runInDurableObject } from 'cloudflare:test';
import { createSessionCookie } from '../src/auth/session';
import { toolRequest } from '../src/mcp-tools';
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('hub'));
it('serves authorized REST and MCP statistics without changing task history', async () => {
  const cookie = (await createSessionCookie(env as any, { sub: 'stats-op', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf', exp: Math.floor(Date.now()/1000)+3600 })).split(';')[0];
  const read = (path: string) => SELF.fetch('https://hub.test' + path, {headers:{Cookie:cookie}});
  const input = {...task([{kind:'note',text:'DONE',captured_at:Date.now()-1000}]),created_at:Date.now()-10000,id:'stats-http-fixture',status:'open',version:1};
  await (runInDurableObject as any)(stub(), (h: any) => {
    h.sql.exec('INSERT OR REPLACE INTO intake_tasks VALUES (?,?)',input.id,JSON.stringify(input));
  });
  const response = await read('/api/intake/tasks/stats-http-fixture/stats');
  expect(response.status).toBe(200);
  const data: any = await response.json();
  expect(data.stats).toMatchObject({id:input.id,done_at:input.history[0].captured_at});
  expect(data.stats.lead_time_ms).toBe(input.history[0].captured_at-input.created_at);
  const listing: any = await (await read('/api/intake/stats?window=all&sort=attempt_count')).json();
  expect(listing.items.find((i:any)=>i.id===input.id)).toBeTruthy();
  expect(listing.aggregates.window).toBe('all');
  expect(toolRequest('https://hub.test', 'intake_stats', {id:input.id}, 'Bearer fixture').url).toBe('https://hub.test/api/intake/tasks/stats-http-fixture/stats');
  expect((await SELF.fetch('https://hub.test/api/intake/stats')).status).toBe(401);
  const retained: any = await (await read('/api/intake/tasks/' + input.id)).json();
  expect(retained.task.payload_data.history).toEqual(input.history);
  expect(retained.task.stats).toBeUndefined();
  const withStats: any = await (await read('/api/intake/tasks/' + input.id + '?include_stats=true')).json();
  expect(withStats.task.stats).toMatchObject({id:input.id,attempt_count:0});
  expect((await read('/api/intake/stats?window=invalid')).status).toBe(400);
  expect((await read('/api/intake/stats?sort=secret')).status).toBe(400);
});

it('does not disclose foreign session telemetry through an intake grant', () => {
  const sources:any = empty();
  sources.sessions.push({id:'foreign',actor:'other',machine_id:'secret-host',engine:'secret-engine',state:'failed',request_json:'{}',binding_json:JSON.stringify({native_id:'private-native',native_created_at_ms:at(5000)})});
  sources.natives.push({machine_id:'secret-host',native_id:'private-native',created_at_ms:at(5000),account:'secret-account'});
  const t=task([{kind:'link',id:'l',hub_session_id:'foreign'}]);
  const result=projectVisibleIntakeStats(t,sources,now,{actor:'reader'});
  expect(result).toMatchObject({attempt_count:1,first_session_started_at:null,failed_attempts:null,accounts:[],unknown_reasons:{failed_attempts:'session_owner_required'}});
  expect(JSON.stringify(result)).not.toMatch(/private-native|secret-host|secret-engine|secret-account/);
  expect(projectVisibleIntakeStats(t,sources,now,{admin:true}).failed_attempts).toBe(1);
});

it('indexed table joins preserve retry history, links, ambiguous natives, and audit', () => {
  const sources:any={...empty(),audit:[{task_id:'task',to_status:'done',captured_at:at(1000)}]};
  sources.sessions=[{id:'s',actor:'owner',machine_id:'a',fleet_launch_id:'new',state:'running',request_json:'{"intake_task_id":"task"}',binding_json:JSON.stringify({native_id:'n',native_created_at_ms:at(5000)})}];
  sources.launches=[{id:'old',retried_launch_id:'new',request_json:'{}',state:'failed'}, {id:'new',request_json:'{"intake_task_id":"task"}',state:'running'}];
  sources.natives=[{machine_id:'a',native_id:'n',created_at_ms:at(5000)},{machine_id:'b',native_id:'n',created_at_ms:at(8000)}];
  const t=task([{kind:'link',id:'l',native_id:'n'}]);
  const indexed=indexStatsSources(sources)(t);
  expect(projectIntakeStats(t,indexed,now)).toEqual(projectIntakeStats(t,sources,now));
  expect(indexStatsSources(sources)({...t,id:'unrelated',history:[]}).sessions).toEqual([]);
});

it('never assigns a later census incarnation to a historical external link', () => {
  const sources:any={...empty(),audit:[]};
  sources.natives=[{machine_id:'m',native_id:'n',created_at_ms:at(1000),engine:'later-engine',account:'later-account'}];
  sources.sessions=[{id:'later',machine_id:'m',engine:'later-engine',state:'running',request_json:'{}',binding_json:JSON.stringify({native_id:'n',native_created_at_ms:at(1000)})}];
  const t=task([{kind:'link',id:'old-link',host:'m',native_id:'n',captured_at:at(90000),verified_at:at(90000),evidence:'external_live_verified'}]);
  const result=projectIntakeStats(t,sources,now);
  expect(result).toMatchObject({attempt_count:1,first_session_started_at:null,accounts:[],engines:[]});
  expect(JSON.stringify(result)).not.toMatch(/later-account|later-engine/);
  expect(projectIntakeStats(t,indexStatsSources(sources)(t),now)).toEqual(result);
});
