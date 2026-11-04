import type { SqlStorage } from '@cloudflare/workers-types';

export function observationModel(value: unknown): string | null {
  return typeof value === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9_./:-]{0,159}$/.test(value) ? value : null;
}

/** Publish identity and lifecycle without inventing an owner or a lock receipt.
 * Only the authenticated host census calls this helper. Operator adoption is
 * a separate transition with an exact native/repository receipt. */
export function insertSessionObservation(sql: SqlStorage, machine: string, native: any,
  state: string, family: string | null | undefined, now: number): void {
  const at = new Date(now).toISOString();
  const created = Number.isSafeInteger(native.created_at_ms) && native.created_at_ms > 0
    && Number.isFinite(new Date(native.created_at_ms).getTime()) ? native.created_at_ms : null;
  const request = { provenance: 'observed_native', origin: 'aplexer', observation_only: true,
    workspace: native.workspace, cwd: native.cwd ?? null, engine: native.engine ?? null,
    tag: native.tag, model: observationModel(native.model), created_at_ms: created };
  const binding = { native_id: native.native_id, tag: native.tag, workspace: native.workspace,
    native_created_at_ms: created, generation: 1, provenance: 'observed_native', observed_at: at };
  sql.exec(`INSERT INTO session_control_sessions
    (id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,
     parent_id,parent_source,session_type,host_family,created_at,updated_at)
    VALUES (?,'unknown',?,?,?,?,?,?,?,?,?,?,?,?,?)`,
    native.native_id, 'native-' + native.native_id, JSON.stringify(request), machine,
    native.engine ?? null, 'interactive', state, JSON.stringify(binding), native.parent_id || null,
    native.parent_id ? 'inferred' : 'unknown_external', 'aplexer', family ?? null,
    created ? new Date(created).toISOString() : at, at);
}

/** Tags are lookup aliases only. Filter ownership before testing ambiguity. */
export function observedHubTagMatches(sql: SqlStorage, ref: string, owns: (row: any) => boolean): any[]&{observationLimited?:boolean} {
  if (!/^[a-f0-9][a-f0-9-]{7,35}$/i.test(ref)) return [];
  const source=sql.exec('SELECT * FROM session_control_sessions ORDER BY id LIMIT 129').toArray() as any[];
  if(source.length>128){const unknown:any[]&{observationLimited?:boolean}=[];unknown.observationLimited=true;return unknown;}
  return source.filter(row=>{
    let request:any,binding:any;try{request=JSON.parse(row.request_json);binding=JSON.parse(row.binding_json);}catch{return false;}
    const tag=binding?.tag;
    return request?.provenance==='observed_native'&&typeof tag==='string'&&tag.toLowerCase().startsWith('hub-'+ref.toLowerCase())
      && /^hub-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(tag)&&owns(row);
  });
}

/** Compact evidence projection, updated on task writes. Historical backfill is
 * keyset-paged and never copies full task bodies into the census handler. */
export function observationIntakeRevision(sql: SqlStorage, machine: string): string {
  const initialized=['session_observation_intake_schema','session_observation_intake_meta','session_observation_intakes']
    .every(name=>sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name=?",name).toArray().length>0)
    && Number((sql.exec('SELECT version FROM session_observation_intake_schema WHERE id=1').toArray()[0] as any)?.version)===2;
  if(!initialized) {
  sql.exec(`CREATE TABLE IF NOT EXISTS session_observation_intake_meta
    (id INTEGER PRIMARY KEY CHECK(id=1), cursor TEXT NOT NULL, complete INTEGER NOT NULL, revision INTEGER NOT NULL)`);
  sql.exec("INSERT OR IGNORE INTO session_observation_intake_meta VALUES(1,'',0,0)");
  sql.exec('CREATE TABLE IF NOT EXISTS session_observation_intakes(task_id TEXT PRIMARY KEY, events TEXT NOT NULL)');
  sql.exec(`CREATE TABLE IF NOT EXISTS session_observation_intake_schema
    (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)`);
  sql.exec('INSERT OR IGNORE INTO session_observation_intake_schema VALUES(1,0)');
  const events = (data: string) => `(SELECT json_group_array(json_object(
    'hub_session_id',json_extract(e.value,'$.hub_session_id'),
    'native_id',json_extract(e.value,'$.native_id'),'host',json_extract(e.value,'$.host'),
    'kind',json_extract(e.value,'$.kind'),'intake_key',json_extract(e.value,'$.intake_key'),
    'text',CASE WHEN json_extract(e.value,'$.kind')='note'
      THEN json_extract(e.value,'$.text') ELSE NULL END))
    FROM json_each(${data},'$.history') e WHERE e.type='object' AND (json_extract(e.value,'$.hub_session_id') IS NOT NULL
      OR json_extract(e.value,'$.native_id') IS NOT NULL
      OR (json_extract(e.value,'$.kind')='note' AND (
        upper(ltrim(json_extract(e.value,'$.text'))) LIKE 'START%'
        OR json_extract(e.value,'$.intake_key') GLOB 'note-aplexer-*'))))`;
  const schema: any = sql.exec('SELECT version FROM session_observation_intake_schema WHERE id=1').toArray()[0];
  const rebuildAplexerNotes = Number(schema?.version ?? 0) < 2;
  if (rebuildAplexerNotes) {
    // Existing deployments have triggers and a compact projection built before
    // note-aplexer events were accepted. Rebuild the projection in bounded
    // pages below, and replace the trigger definitions before recording v2.
    sql.exec('DROP TRIGGER IF EXISTS session_observation_intake_insert');
    sql.exec('DROP TRIGGER IF EXISTS session_observation_intake_update');
    sql.exec('DROP TRIGGER IF EXISTS session_observation_intake_delete');
    sql.exec("UPDATE session_observation_intake_meta SET cursor='',complete=0,revision=revision+1 WHERE id=1");
  }
  for (const op of ['INSERT','UPDATE']) sql.exec(`CREATE TRIGGER IF NOT EXISTS session_observation_intake_${op.toLowerCase()}
    AFTER ${op} ON intake_tasks BEGIN
      INSERT INTO session_observation_intakes(task_id,events) VALUES(NEW.id,${events('NEW.data')})
        ON CONFLICT(task_id) DO UPDATE SET events=excluded.events;
      UPDATE session_observation_intake_meta SET revision=revision+1 WHERE id=1;
    END`);
  sql.exec(`CREATE TRIGGER IF NOT EXISTS session_observation_intake_delete AFTER DELETE ON intake_tasks BEGIN
    DELETE FROM session_observation_intakes WHERE task_id=OLD.id;
    UPDATE session_observation_intake_meta SET revision=revision+1 WHERE id=1; END`);
  if (rebuildAplexerNotes) sql.exec('UPDATE session_observation_intake_schema SET version=2 WHERE id=1');
  }
  let meta: any = sql.exec('SELECT * FROM session_observation_intake_meta WHERE id=1').toArray()[0];
  if (!meta.complete) {
    // Read a raw task page before expanding history. json_each over100task
    // histories made the formerly bounded-looking page expensive in SQL.
    const source=sql.exec('SELECT id,data FROM intake_tasks WHERE id>? ORDER BY id LIMIT 16',meta.cursor).toArray() as any[];
    const batch:any[]=[];
    for(const task of source) {
      let data:any;try{data=JSON.parse(task.data);}catch{return `${meta.revision}/0/history_unreadable`;}
      const history=data?.history??[];
      if(!Array.isArray(history)||history.length>128)return `${meta.revision}/0/history_limited`;
      const events=history.filter((e:any)=>e&&typeof e==='object'&&!Array.isArray(e)&&(e.hub_session_id!=null||e.native_id!=null
        ||e.kind==='note'&&(/^START/i.test(String(e.text??'').replace(/^ +/,''))||String(e.intake_key??'').startsWith('note-aplexer-'))))
        .map((e:any)=>({hub_session_id:e.hub_session_id??null,native_id:e.native_id??null,host:e.host??null,kind:e.kind??null,intake_key:e.intake_key??null,text:e.kind==='note'?e.text??null:null}));
      batch.push({id:task.id,events:JSON.stringify(events)});
    }
    for (const task of batch) sql.exec(`INSERT INTO session_observation_intakes(task_id,events) VALUES(?,?)
      ON CONFLICT(task_id) DO UPDATE SET events=excluded.events`, task.id, task.events);
    sql.exec('UPDATE session_observation_intake_meta SET cursor=?,complete=?,revision=revision+1 WHERE id=1',
      batch.length ? batch[batch.length-1].id : meta.cursor, batch.length < 16 ? 1 : 0);
    meta = sql.exec('SELECT * FROM session_observation_intake_meta WHERE id=1').toArray()[0];
  }
  const hasWorktrees = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='intake_worktrees'").toArray().length;
  const trees=hasWorktrees?sql.exec(`SELECT task_id,path,state,updated_at FROM intake_worktrees INDEXED BY intake_worktrees_path
    WHERE machine=? ORDER BY path,rowid LIMIT 129`,machine).toArray() as any[]:[];
  const active=trees.filter(row=>row.state==='active');
  return `${meta.revision}/${meta.complete}/${active.length}/${active.map(row=>String(row.updated_at)).sort().at(-1)??''}/${trees.length>128?'worktrees_limited':''}`;
}

/** Exact token matching remains in JS; SQL only prefilters compact evidence.
 * Ambiguity and incomplete backfill fail closed rather than choosing a task. */
export function observedIntakeCandidates(sql: SqlStorage, machine: string, rows: any[]): Map<string, string>&{observationLimited?:boolean} {
  const links:Map<string,string>&{observationLimited?:boolean}=new Map<string,string>();
  if (!rows.length) return links;
  const meta: any = sql.exec('SELECT complete FROM session_observation_intake_meta WHERE id=1').toArray()[0];
  if (!meta?.complete) {links.observationLimited=true;return links;}
  const hasWorktrees = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='intake_worktrees'").toArray().length;
  // Observe the complete compact source once, before text matching. An
  // incomplete set must never manufacture unique linkage from an old prefix.
  const candidates=sql.exec('SELECT task_id,events FROM session_observation_intakes ORDER BY task_id LIMIT 129').toArray();
  const trees=hasWorktrees?sql.exec(`SELECT task_id,path,state,updated_at FROM intake_worktrees INDEXED BY intake_worktrees_path
    WHERE machine=? ORDER BY path,rowid LIMIT 129`,machine).toArray() as any[]:[];
  if(candidates.length>128||trees.length>128){links.observationLimited=true;return links;}
  const taskIds=new Set(candidates.map(row=>String(row.task_id)));
  const parsedEvents=new Map<string,any[]>();
  for(const task of candidates) {
    let events:any;try{events=JSON.parse(String(task.events));}catch{links.observationLimited=true;return links;}
    if(!Array.isArray(events)||events.length>128){links.observationLimited=true;return links;}
    parsedEvents.set(String(task.task_id),events);
  }
  for (const row of rows) {
    const binding = JSON.parse(row.binding_json);
    const ids = new Set([row.id, binding.native_id].filter(Boolean));
    const matches = new Set<string>();
    for (const task of candidates) {
      const events = parsedEvents.get(String(task.task_id))!;
      for (const event of events) {
        if (ids.has(event.hub_session_id) || (ids.has(event.native_id) && (!event.host || event.host === machine))) {
          matches.add(String(task.task_id)); break;
        }
        if (event.kind !== 'note' || typeof event.text !== 'string') continue;
        const nativeKey = typeof event.intake_key === 'string'
          ? /^note-aplexer-([a-f0-9-]{8,36})$/i.exec(event.intake_key)?.[1] : undefined;
        const keyedAplexerStart = !!nativeKey && [...ids].some(id =>
          String(id).toLowerCase().startsWith(nativeKey.toLowerCase()));
        if (!/^START(?:ED)?\b/i.test(event.text.trim()) && !keyedAplexerStart) continue;
        const tokens: string[] = event.text.split(/[^A-Za-z0-9_.:/~-]+/);
        if (tokens.some(token => ids.has(token) || token === binding.tag || token.endsWith(':' + binding.tag))) {
          matches.add(String(task.task_id)); break;
        }
      }
    }
    for(const tree of trees)if(tree.state==='active'&&tree.path===binding.workspace&&taskIds.has(String(tree.task_id)))matches.add(String(tree.task_id));
    if (matches.size === 1) links.set(row.id,[...matches][0]);
  }
  return links;
}
