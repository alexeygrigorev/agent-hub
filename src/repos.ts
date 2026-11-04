import { effectiveHost } from './registry';
/** Private repository registry. Human fields are insert-only during discovery. */
import { boundedText } from './mcp';
import { decodeRepoWorkspace, encodeRepoWorkspace } from './fleet';
import { advertisedRepoPaths, allowedMachinesValid, repoPathKey } from './repo-host-policy';
import type { SqlStorage } from '@cloudflare/workers-types';

type Auth = { caller?: { kind?: string; isAdmin?: boolean; machineId?: string }; scopes?: string[]; error?: Response };
const human = ['label', 'purpose', 'stack', 'key_folders', 'urls', 'aliases', 'category', 'description', 'preferred_workspace_subdir', 'clone_policy', 'seed_data', 'allowed_machines'] as const;
const operatorFields = [...human, 'status', 'canonical', 'portable'] as const;
const checkoutFields = ['workspace_root', 'checkout_status', 'branch', 'dirty_counts', 'disk_free_bytes', 'cloned_on_demand'] as const;
const machineFields = ['dir', 'origin', 'org', 'default_branch', 'status', 'canonical', 'last_commit', 'workflows', 'github_url', 'flags'] as const;
const nameOk = (v: any) => typeof v === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(v);
const textOk = (v: any) => typeof v === 'string' && v.length <= 4096 && !/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(v);
const pathOk = (v: any) => typeof v === 'string' && v.length <= 512 && /^(\/|[A-Za-z]:[\\/])/.test(v) && !/[\x00-\x1f\x7f]/.test(v) && !v.split(/[\\/]/).some((p: string) => p === '..' || p === '.');
const json = (v: any, status = 200) => Response.json(v, { status, headers: { 'Cache-Control': 'private, no-store' } });

const foldRepoName=(name:string)=>name.replace(/[A-Z]/g,value=>value.toLowerCase());
const repoObservation=(code:string)=>Object.assign(new Error(code),{code,status:503});

/** Friendly registered aliases may contain spaces, Unicode or a descriptive slash.
 * They never become paths: only the registry's canonical absolute dir is executable. */
export function registeredRepoLabel(sql: SqlStorage, value: unknown): boolean {
  if (typeof value !== 'string' || !value || value.length > 128 || /[\x00-\x1f\x7f]/.test(value)) return false;
  try {
    let table='repo_registry',pendingLegacy=false;
    if(sql.exec("SELECT name FROM pragma_table_list('repo_registry_upgrade') WHERE schema='main' AND type='table'").toArray().length){
      const state=sql.exec('SELECT /* live repo upgrade */ done,after_name,source_revision,start_revision FROM repo_registry_upgrade WHERE id=1').toArray()[0] as any;
      if(state&&(state.done||state.after_name!==null)&&state.source_revision!==state.start_revision)throw repoObservation('repo_registry_upgrade_source_changed');
      if(state?.done)table='repo_registry_v2';else pendingLegacy=true;
    }
    const rows=sql.exec(`SELECT data FROM ${table} ORDER BY name LIMIT 257`).toArray();
    if(rows.length>256||rows.reduce((sum,row)=>sum+String(row.data).length*2,0)>2*1024*1024)throw repoObservation('repo_registry_observation_limited');
    const ids=new Set<string>(),names=new Set<string>();let matched=false;
    for(const row of rows){
      const repo=JSON.parse(String(row.data));let id=repo.repo_id,name=repo.name;
      if(pendingLegacy){
        if(typeof repo?.name!=='string'||!repo.name||repo.name.length>128||repo.origin!=null&&typeof repo.origin!=='string')throw repoObservation('repo_registry_invalid_legacy');
        id=repo.status==='duplicate'||!repo.origin?'local/'+repo.name.toLowerCase():repo.origin.toLowerCase();
        if(ids.has(id)){const base='local/'+repo.name.toLowerCase();id=base;let suffix=1;while(ids.has(id))id=base+'-legacy-'+suffix++;}
        ids.add(id);
        if(names.has(foldRepoName(name))){let suffix=1;name=repo.name.slice(0,118);while(names.has(foldRepoName(name)))name=repo.name.slice(0,118)+'-'+suffix++;}
        names.add(foldRepoName(name));
      }
      matched ||= [id,name,repo.label,...(Array.isArray(repo.aliases)?repo.aliases:[])].some(v=>typeof v==='string'&&v.toLowerCase()===value.toLowerCase());
    }
    return matched;
  } catch(error) {if((error as any)?.status===503)throw error;return false;}
}

export class RepoRegistry {
  private readonly sourceSql:SqlStorage;
  private legacy=false;
  private table:'repo_registry'|'repo_registry_v2'='repo_registry';
  constructor(private sql: SqlStorage, private txn: <T>(fn: () => T) => T, private now: () => number = Date.now) {this.sourceSql=sql;}
  init() {
    this.sql=this.sourceSql;
    this.txn(() => {
      const columns=this.sourceSql.exec('PRAGMA table_info(repo_registry)').toArray();
      this.legacy=columns.length>0&&!columns.some(c=>c.name==='repo_id');
      const table=this.legacy?'repo_registry_v2':'repo_registry';this.table=table;
      // Original single-host data remains untouched. An empty destination and
      // cursor are cheap; only explicit authorized repo requests copy pages.
      this.sourceSql.exec(`CREATE TABLE IF NOT EXISTS ${table} (
        repo_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        origin TEXT NOT NULL DEFAULT '', origin_url TEXT NOT NULL DEFAULT '', default_branch TEXT NOT NULL DEFAULT '',
        category TEXT, description TEXT, preferred_workspace_subdir TEXT, clone_policy TEXT, seed_data TEXT,
        data TEXT NOT NULL, updated_at TEXT NOT NULL)`);
      this.sourceSql.exec(`CREATE UNIQUE INDEX IF NOT EXISTS idx_${table}_name_nocase ON ${table}(LOWER(name))`);
      this.sourceSql.exec(`CREATE TABLE IF NOT EXISTS repo_checkouts (
        repo_id TEXT NOT NULL, host_id TEXT NOT NULL, path TEXT, workspace_root TEXT,
        status TEXT NOT NULL, last_seen TEXT, head_sha TEXT, head_date TEXT, branch TEXT,
        dirty_counts TEXT, disk_free_bytes INTEGER, cloned_on_demand INTEGER NOT NULL DEFAULT 0,
        last_used_at TEXT, flags_json TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL,
        PRIMARY KEY(repo_id, host_id))`);
      this.sourceSql.exec('CREATE INDEX IF NOT EXISTS idx_repo_checkouts_host ON repo_checkouts(host_id)');
      this.sourceSql.exec('CREATE INDEX IF NOT EXISTS idx_repo_checkouts_path ON repo_checkouts(host_id,path)');
      if(this.legacy){
        this.sourceSql.exec('CREATE TABLE IF NOT EXISTS repo_registry_upgrade(id INTEGER PRIMARY KEY CHECK(id=1),after_name TEXT,done INTEGER NOT NULL,source_revision INTEGER NOT NULL,start_revision INTEGER NOT NULL)');
        this.sourceSql.exec('INSERT OR IGNORE INTO repo_registry_upgrade VALUES(1,NULL,0,0,0)');
        for(const op of ['INSERT','UPDATE','DELETE'])this.sourceSql.exec(`CREATE TRIGGER IF NOT EXISTS repo_registry_upgrade_${op.toLowerCase()} AFTER ${op} ON repo_registry BEGIN UPDATE repo_registry_upgrade SET source_revision=source_revision+1 WHERE id=1; END`);
      }
    });
  }

  private upgradeState():any {
    if(!this.legacy)return {done:1};
    const state=this.sourceSql.exec('SELECT /* live repo upgrade */ * FROM repo_registry_upgrade WHERE id=1').toArray()[0] as any;
    if(!state)throw repoObservation('repo_registry_upgrade_unavailable');
    if((state.done||state.after_name!==null)&&state.source_revision!==state.start_revision)throw repoObservation('repo_registry_upgrade_source_changed');
    return state;
  }
  private legacyRecords():{source_name:string;repo:any}[] {
    const rows=this.sourceSql.exec('SELECT /* live legacy repos */ name,machine,data,updated_at FROM repo_registry ORDER BY name LIMIT 257').toArray() as any[];
    if(rows.length>256)throw repoObservation('repo_registry_observation_limited');
    if(rows.reduce((sum,row)=>sum+String(row.data).length*2,0)>2*1024*1024)throw repoObservation('repo_registry_observation_limited');
    const names=new Set<string>(),ids=new Set<string>();
    return rows.map(row=>{
      let old:any;try{old=JSON.parse(row.data);}catch{throw repoObservation('repo_registry_invalid_legacy');}
      if(typeof old?.name!=='string'||!old.name||old.name.length>128||old.origin!=null&&typeof old.origin!=='string')throw repoObservation('repo_registry_invalid_legacy');
      let id=this.identity(old),name=old.name;
      if(ids.has(id)){const base='local/'+old.name.toLowerCase();id=base;let suffix=1;while(ids.has(id))id=base+'-legacy-'+suffix++;}
      if(names.has(foldRepoName(name))){let suffix=1;name=old.name.slice(0,118);while(names.has(foldRepoName(name)))name=old.name.slice(0,118)+'-'+suffix++;}
      ids.add(id);names.add(foldRepoName(name));
      return{source_name:row.name,repo:{...old,name,repo_id:id,machine:old.machine??row.machine,updated_at:old.updated_at??row.updated_at}};
    });
  }
  private advanceLegacy():{ready:boolean;copied:number} {
    return this.txn(()=>{
      const state=this.upgradeState();if(state.done)return{ready:true,copied:0};
      const all=this.legacyRecords(),after=state.after_name===null?-1:all.findIndex(row=>row.source_name===state.after_name);
      if(state.after_name!==null&&after<0)throw repoObservation('repo_registry_upgrade_source_changed');
      const remaining=all.slice(after+1),page=remaining.slice(0,16);
      for(const row of page){this.save(row.repo,row.repo.updated_at);this.saveCheckout(row.repo.repo_id,row.repo,row.repo.updated_at);}
      const done=remaining.length<=16;
      const changed=this.sourceSql.exec('UPDATE repo_registry_upgrade SET after_name=?,done=?,start_revision=? WHERE id=1 AND source_revision=? RETURNING id',page.at(-1)?.source_name??state.after_name,done?1:0,state.source_revision,state.source_revision).toArray();
      if(changed.length!==1)throw repoObservation('repo_registry_upgrade_source_changed');
      return{ready:done,copied:page.length};
    });
  }
  private prepareLegacy():Response|null {
    if(!this.legacy)return null;
    try{const progress=this.advanceLegacy();return progress.ready?null:json({error:'repo_registry_upgrade_pending',copied:progress.copied,retry:true},503);}
    catch(error){return json({error:(error as any)?.code??'repo_registry_upgrade_unavailable'},503);}
  }
  private identity(repo: any): string {
    return repo.status === 'duplicate' || !repo.origin ? 'local/' + repo.name.toLowerCase() : repo.origin.toLowerCase();
  }
  private availableName(base: string): string {
    let candidate = base.slice(0, 118), i = 1;
    while (this.named(candidate)) candidate = base.slice(0, 118) + '-' + i++;
    return candidate;
  }
  all(): any[] {
    const machineRows=this.sql.exec('SELECT id,last_checkin_at,capabilities FROM machines ORDER BY id LIMIT 65').toArray();
    if(machineRows.length>64)throw repoObservation('repo_registry_observation_limited');
    if(machineRows.reduce((sum,row)=>sum+String(row.capabilities??'').length*2,0)>2*1024*1024)throw repoObservation('repo_registry_observation_limited');
    const machines=machineRows.map(m=>effectiveHost(this.sql,m));
    const online = new Set(machines
      .filter(m => { const age = this.now() - Date.parse(String(m.last_checkin_at)); return age >= 0 && age <= 180000; }).map(m => String(m.id)));
    const pending=!this.upgradeState().done;
    const source=pending?this.legacyRecords().map(row=>({repo_id:row.repo.repo_id,data:JSON.stringify(row.repo)})):this.sql.exec(`SELECT repo_id,data FROM ${this.table} ORDER BY name LIMIT 257`).toArray();
    if(source.length>256||source.reduce((sum,row)=>sum+String(row.data).length*2,0)>2*1024*1024)throw repoObservation('repo_registry_observation_limited');
    return source.map(row => {
      const repo = { ...JSON.parse(String(row.data)), repo_id: row.repo_id };
      const checkoutRows=this.sql.exec('SELECT * FROM repo_checkouts WHERE repo_id=? ORDER BY host_id LIMIT 65',row.repo_id).toArray();
      if(checkoutRows.length>64||checkoutRows.reduce((sum,c)=>sum+String(c.dirty_counts??'').length*2+String(c.flags_json??'').length*2,0)>2*1024*1024)throw repoObservation('repo_registry_observation_limited');
      const checkouts: any[] = checkoutRows.map(c => ({
        ...c, machine_id: c.host_id, local_path: c.path, cloned_on_demand: !!c.cloned_on_demand,
        dirty_counts: c.dirty_counts ? JSON.parse(String(c.dirty_counts)) : null,
        flags: JSON.parse(String(c.flags_json)), online: online.has(String(c.host_id)),
      }));
      if(checkouts.length>64)throw repoObservation('repo_registry_observation_limited');
      if(pending&&repo.machine&&!checkouts.some(c=>c.host_id===repo.machine))checkouts.push({host_id:repo.machine,machine_id:repo.machine,path:repo.dir??null,local_path:repo.dir??null,
        status:repo.flags?.includes('WORKTREE')?'worktree_only':repo.flags?.includes('MISSING')?'missing':repo.flags?.includes('GIT_ERROR')?'error':'present',
        last_seen:repo.updated_at,updated_at:repo.updated_at,workspace_root:repo.workspace_root??null,last_used_at:repo.cloned_on_demand===true?repo.updated_at:null,flags:repo.flags??[],branch:repo.branch??null,head_sha:repo.last_commit?.sha??null,head_date:repo.last_commit?.date??null,
        dirty_counts:repo.dirty_counts??null,cloned_on_demand:repo.cloned_on_demand===true,online:online.has(repo.machine),source:'legacy_registry'});
      // Hosts already advertising a checkout need no operator registry edit.
      // A scanned row, including an explicit missing/error result, takes precedence.
      for (const machine of machines) {
        if (checkouts.some(c => c.host_id === machine.id)) continue;
        let caps: any;
        try { caps = JSON.parse(String(machine.capabilities || '{}')); } catch { continue; }
        const paths = advertisedRepoPaths(caps, repo.name);
        if (paths.length !== 1) continue;
        checkouts.push({host_id:machine.id, machine_id:machine.id, path:paths[0], local_path:paths[0],
          status:'present', last_seen:machine.last_checkin_at, flags:[], online:online.has(String(machine.id)),
          source:'host_workspaces', cloned_on_demand:false});
      }
      if(checkouts.length>64)throw repoObservation('repo_registry_observation_limited');
      const representative = checkouts.find(c => c.status === 'present' && c.online) || checkouts.find(c => c.status === 'present') || checkouts[0];
      const present = checkouts.filter(c => c.status === 'present');
      const noOnline = !present.some(c => c.online);
      return { ...repo, machine: representative?.host_id ?? null, dir: representative?.path ?? null,
        checkouts, checkouts_by_host:Object.fromEntries(checkouts.map(c => [c.host_id, c])),
        hosts: present.map(c => c.host_id), present_online_count: present.filter(c => c.online).length,
        no_present_online: noOnline,
        flags: Array.from(new Set([...(repo.flags || []), ...checkouts.flatMap(c => c.flags), ...(noOnline ? ['NO_PRESENT_ONLINE'] : [])])) };
    });
  }
  get(name: string): any | undefined {
    if (typeof name !== 'string') return undefined;
    const rows = this.all();
    const exact = rows.find(r => r.name.toLowerCase() === name.toLowerCase()) || rows.find(r => r.repo_id === name.toLowerCase());
    if (exact) return exact;
    const matches = rows.filter(r => r.label?.toLowerCase() === name.toLowerCase() || r.aliases?.some((a: string) => a.toLowerCase() === name.toLowerCase()));
    if (matches.length > 1) throw new Error('ambiguous_repo');
    return matches[0];
  }
  canonical(repo: any): any {
    const visited = new Set<string>();
    while (repo?.status === 'duplicate') {
      if (visited.has(repo.name)) throw new Error('duplicate_cycle');
      visited.add(repo.name);
      repo = this.get(repo.canonical);
    }
    if (!repo) throw new Error('canonical_repo_missing');
    return repo;
  }
  /** Resolve a clone-on-host --repo slug only when it names a registered active origin. */
  resolveOrigin(value: unknown): { repo?: string; error?: any } {
    if (typeof value !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9._-]{1,100}$/.test(value) || value.split('/')[1].startsWith('.')) {
      return { error: { error: 'invalid_repo' } };
    }
    const requested = value.toLowerCase();
    const rows = this.all();
    const originRows = rows.filter(r => typeof r.origin === 'string' && /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(r.origin));
    const exact = originRows.filter(r => r.origin.toLowerCase() === requested);
    const active = exact.filter(r => r.status === 'active');
    if (active.length === 1) return { repo: active[0].origin };
    if (active.length > 1) return { error: { error: 'ambiguous_repo', repo: value } };
    if (exact.length) return { error: { error: 'repo_unavailable', repo: value } };

    const [owner, name] = value.split('/');
    // A common mistake is to form owner/name from the local registry name,
    // even though that checkout's registered GitHub origin has another name.
    const sameNamed = rows.find(r => r.name.toLowerCase() === name.toLowerCase() &&
      String(r.org || r.origin?.split('/')[0] || '').toLowerCase() === owner.toLowerCase());
    if (sameNamed) {
      try {
        const canonical = this.canonical(sameNamed);
        if (canonical.status === 'active' && typeof canonical.origin === 'string' && canonical.origin) {
          return { error: { error: 'unknown_repo', repo: value, matches: [canonical.origin] } };
        }
      } catch { /* report the bounded unavailable result below */ }
      return { error: { error: 'repo_unavailable', repo: value } };
    }

    const candidates = originRows.filter(r => r.status === 'active');
    const sameOwner = candidates.filter(r => r.origin.split('/')[0].toLowerCase() === owner.toLowerCase());
    const pool = sameOwner.length ? sameOwner : candidates;
    const needle = value.toLowerCase();
    const distance = (candidate: string) => {
      let prev = Array.from({ length: candidate.length + 1 }, (_, i) => i);
      for (let i = 0; i < needle.length; i++) {
        const next = [i + 1];
        for (let j = 0; j < candidate.length; j++) next.push(Math.min(next[j] + 1, prev[j + 1] + 1, prev[j] + (needle[i] === candidate[j] ? 0 : 1)));
        prev = next;
      }
      return prev[candidate.length];
    };
    const matches = [...new Set(pool.map(r => r.origin))]
      .sort((a, b) => distance(a.toLowerCase()) - distance(b.toLowerCase()) || a.localeCompare(b))
      .slice(0, 5);
    return { error: { error: 'unknown_repo', repo: value, ...(matches.length ? { matches } : {}) } };
  }
  private named(name: string): any | undefined {
    const row = this.sql.exec(`SELECT repo_id,data FROM ${this.table} WHERE name = ? COLLATE NOCASE`, name).toArray()[0];
    return row ? { ...JSON.parse(String(row.data)), repo_id: row.repo_id } : undefined;
  }
  private save(repo: any, at?: string) {
    const shared = { ...repo, origin_url: repo.origin || '', updated_at: at || new Date().toISOString() };
    for (const key of ['machine', 'dir', 'checkouts', 'checkouts_by_host', 'hosts', 'no_present_online', 'present_online_count', ...checkoutFields]) delete shared[key];
    this.sql.exec(`INSERT INTO ${this.table} (repo_id,name,origin,origin_url,default_branch,category,description,preferred_workspace_subdir,clone_policy,seed_data,data,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(repo_id) DO UPDATE SET name=excluded.name,origin=excluded.origin,origin_url=excluded.origin_url,default_branch=excluded.default_branch,category=excluded.category,description=excluded.description,preferred_workspace_subdir=excluded.preferred_workspace_subdir,clone_policy=excluded.clone_policy,seed_data=excluded.seed_data,data=excluded.data,updated_at=excluded.updated_at`,
      shared.repo_id, shared.name, shared.origin || '', shared.origin_url, shared.default_branch || '', shared.category ?? null, shared.description ?? null, shared.preferred_workspace_subdir ?? null, shared.clone_policy ?? null, shared.seed_data ?? null, JSON.stringify(shared), shared.updated_at);
  }
  private saveCheckout(id: string, r: any, at?: string) {
    const updated = at || new Date().toISOString();
    const status = r.checkout_status?.toLowerCase() || (r.flags?.includes('WORKTREE') ? 'worktree_only' : r.flags?.includes('MISSING') ? 'missing' : r.flags?.includes('GIT_ERROR') ? 'error' : 'present');
    this.sql.exec(`INSERT INTO repo_checkouts (repo_id,host_id,path,workspace_root,status,last_seen,head_sha,head_date,branch,dirty_counts,disk_free_bytes,flags_json,updated_at,cloned_on_demand,last_used_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(repo_id,host_id) DO UPDATE SET
      path=excluded.path,workspace_root=excluded.workspace_root,status=excluded.status,
      last_seen=CASE WHEN excluded.status='present' THEN excluded.last_seen ELSE repo_checkouts.last_seen END,
      head_sha=COALESCE(excluded.head_sha,repo_checkouts.head_sha),head_date=COALESCE(excluded.head_date,repo_checkouts.head_date),
      branch=COALESCE(excluded.branch,repo_checkouts.branch),dirty_counts=excluded.dirty_counts,
      disk_free_bytes=excluded.disk_free_bytes,flags_json=excluded.flags_json,updated_at=excluded.updated_at,
      cloned_on_demand=CASE WHEN excluded.path=repo_checkouts.path THEN MAX(repo_checkouts.cloned_on_demand,excluded.cloned_on_demand) ELSE excluded.cloned_on_demand END,
      last_used_at=CASE WHEN excluded.cloned_on_demand=1 THEN excluded.updated_at ELSE repo_checkouts.last_used_at END`,
      id, r.machine, r.dir, r.workspace_root ?? null, status, status === 'present' ? updated : null,
      r.last_commit?.sha ?? null, r.last_commit?.date ?? null, r.branch ?? null,
      r.dirty_counts == null ? null : JSON.stringify(r.dirty_counts), r.disk_free_bytes ?? null, JSON.stringify(r.flags || []), updated,
      r.cloned_on_demand === true ? 1 : 0, r.cloned_on_demand === true ? updated : null);
  }
  private upsert(r: any, seed: boolean) {
    let old = this.named(r.name);
    const origin = r.origin || r.origin_url;
    const desired = this.identity({ ...r, origin });
    const sameOrigin = !seed && !old && origin && r.status !== 'duplicate'
      && (this.sql.exec(`SELECT data,repo_id FROM ${this.table} WHERE repo_id=?`, desired).toArray()[0] as any);
    // Host scans (refresh) link an unknown name to the entry for the same
    // remote; a seed names its own entry and never merges into another name.
    if (sameOrigin) {
      const linked = { ...JSON.parse(String(sameOrigin.data)), repo_id: sameOrigin.repo_id };
      if (linked.status !== 'duplicate') old = linked;
    }
    // Different remotes using the same short name cannot overwrite one another.
    if (old && origin && old.origin && old.origin.toLowerCase() !== origin.toLowerCase()) old = undefined;
    if (seed && old) return;
    const name = old?.name || (this.named(r.name) ? this.availableName(origin ? origin.replace('/', '-') : r.name) : r.name);
    let id = old?.repo_id || desired;
    // A distinct entry whose canonical identity is already taken (a second
    // name for the same remote, needed for duplicate classification) keeps
    // its own row under a local identity instead of overwriting the other
    // entry through the shared primary key.
    if (!old && (this.sql.exec(`SELECT repo_id FROM ${this.table} WHERE repo_id=?`, id).toArray().length)) {
      id = 'local/' + String(r.name).toLowerCase();
      let suffix = 1;
      while (this.sql.exec(`SELECT repo_id FROM ${this.table} WHERE repo_id=?`, id).toArray().length) id = 'local/' + String(r.name).toLowerCase() + '-' + (++suffix);
    }
    if (old?.repo_id.startsWith('local/') && origin && old.status !== 'duplicate') {
      id = desired;
      this.sql.exec('UPDATE repo_checkouts SET repo_id=? WHERE repo_id=?', id, old.repo_id);
      this.sql.exec(`DELETE FROM ${this.table} WHERE repo_id=?`, old.repo_id);
    }
    const next = old ? { ...old } : { name, label: name, purpose: '', stack: '', key_folders: '', urls: '', aliases: [], status: 'active', default_branch: '', workflows: '', github_url: '', last_commit: null, canonical: null, flags: ['NEW'] };
    for (const k of [...machineFields, ...(seed ? human : [])]) {
      if (k !== 'dir' && k !== 'flags' && r[k] !== undefined && r[k] !== null && (k !== 'origin' && k !== 'default_branch' || r[k] !== '')) next[k] = r[k];
    }
    if (origin) next.origin = origin;
    if (seed && r.flags) next.flags = r.flags.filter((f: string) => !['MISSING', 'MOVED', 'WORKTREE', 'GIT_ERROR'].includes(f));
    if (!old && name !== r.name) next.flags = [...next.flags, 'NAME_COLLISION'];
    if (old?.status === 'duplicate') { next.status = 'duplicate'; next.canonical = old.canonical; }
    next.name = name; next.repo_id = id;
    this.save(next);
    this.saveCheckout(id, r);
  }
  private valid(repo: any, seed: boolean): boolean {
    if (!repo || !nameOk(repo.name) || !nameOk(repo.machine) || !pathOk(repo.dir)) return false;
    const allowed = new Set<string>(['name', 'machine', ...machineFields, ...checkoutFields, 'origin_url', ...(seed ? human : [])]);
    if (Object.keys(repo).some(k => !allowed.has(k))) return false;
    if (seed && human.some(k => !['aliases', 'allowed_machines'].includes(k) && repo[k] !== undefined && !textOk(repo[k]))) return false;
    if (repo.allowed_machines !== undefined && !allowedMachinesValid(repo.allowed_machines)) return false;
    if (repo.aliases !== undefined && (!Array.isArray(repo.aliases) || repo.aliases.length > 32 || repo.aliases.some((a: any) => !textOk(a) || !a.trim() || a.length > 128))) return false;
    for (const k of ['origin', 'org', 'default_branch', 'canonical', 'github_url', 'workflows']) if (repo[k] !== undefined && repo[k] !== null && !textOk(repo[k])) return false;
    if (repo.origin && !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo.origin)) return false;
    if (repo.status !== undefined && !['active', 'archived', 'duplicate'].includes(repo.status)) return false;
    if (repo.status === 'duplicate' && !nameOk(repo.canonical)) return false;
    if (repo.last_commit !== undefined && repo.last_commit !== null && (!repo.last_commit || !/^[a-f0-9]{40,64}$/.test(repo.last_commit.sha) || typeof repo.last_commit.date !== 'string' || !Number.isFinite(Date.parse(repo.last_commit.date)))) return false;
    if (repo.flags !== undefined && (!Array.isArray(repo.flags) || repo.flags.some((f: any) => !['NEW', 'MOVED', 'MISSING', 'GH_ERROR', 'GIT_ERROR', 'WORKTREE'].includes(f)))) return false;
    if (repo.origin_url != null && (typeof repo.origin_url !== 'string' || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo.origin_url))) return false;
    if (repo.origin && repo.origin_url && repo.origin.toLowerCase() !== repo.origin_url.toLowerCase()) return false;
    if (repo.workspace_root != null && !pathOk(repo.workspace_root)) return false;
    if (repo.cloned_on_demand !== undefined && typeof repo.cloned_on_demand !== 'boolean') return false;
    if (repo.checkout_status != null && !['present','missing','cloning','error','worktree_only'].includes(String(repo.checkout_status).toLowerCase())) return false;
    if (repo.branch != null && !textOk(repo.branch)) return false;
    if (repo.disk_free_bytes != null && (!Number.isSafeInteger(repo.disk_free_bytes) || repo.disk_free_bytes < 0)) return false;
    if (repo.dirty_counts != null && (typeof repo.dirty_counts !== 'object' || Array.isArray(repo.dirty_counts) || Object.entries(repo.dirty_counts).some(([k,v]) => !['staged','unstaged','untracked'].includes(k) || !Number.isSafeInteger(v) || Number(v) < 0))) return false;
    return true;
  }
  /** Resolve catalog identity; placement chooses the host and checkout later. */
  resolveLaunchRepo(input: any): { input?: any; error?: any } {
    // Repo-identity workspaces (intake 8028bb06) are resolved by each host, not by this registry.
    if (typeof input.workspace !== 'string' || decodeRepoWorkspace(input.workspace)) return { input };
    const rows = this.all();
    // Empty installations preserve configured legacy workspaces until seeded.
    if (!rows.length) return { input };
    if (pathOk(input.workspace)) {
      const key = repoPathKey(input.workspace);
      const matches = rows.filter(r => r.checkouts.some((c: any) =>
        (!input.machine || c.host_id === input.machine) && c.status === 'present' && repoPathKey(c.path) === key));
      if (!matches.length) return { input };
      let repo: any;
      try {
        const canonical = matches.map(r => this.canonical(r));
        if (new Set(canonical.map(r => r.repo_id)).size !== 1) return {error:{error:'ambiguous_repo'}};
        repo = canonical[0];
      } catch (e) { return {error:{error:(e as Error).message}}; }
      if (repo.status === 'archived') return { error: { error: 'repo_archived' } };
      const summary = this.summary(repo);
      return {input:{...input,
        repo_context:{repo_id:repo.repo_id, name:repo.name, explicit_path:input.workspace,
          allowed_machines:repo.allowed_machines ?? null},
        ...(input.prompt ? {prompt:input.prompt.endsWith(summary) ? input.prompt : input.prompt + summary} : {})}};
    }
    let repo: any;
    try { repo = this.get(input.workspace); } catch { return { error: { error: 'ambiguous_repo' } }; }
    if (!repo) {
      // Existing explicit configured labels remain a supported interface.
      const machines = this.sql.exec('SELECT id,capabilities FROM machines').toArray();
      if (machines.some(m => (!input.machine || m.id === input.machine) && (() => { try { return JSON.parse(String(m.capabilities || '{}')).workspaces?.includes(input.workspace); } catch { return false; } })())) return { input };
      const names = rows.map(r => r.name);
      const needle = input.workspace.toLowerCase();
      const distance = (s: string) => {
        let prev = Array.from({ length: s.length + 1 }, (_, i) => i);
        for (let i = 0; i < needle.length; i++) {
          const next = [i + 1];
          for (let j = 0; j < s.length; j++) next.push(Math.min(next[j] + 1, prev[j + 1] + 1, prev[j] + (needle[i] === s[j] ? 0 : 1)));
          prev = next;
        }
        return prev[s.length];
      };
      return { error: { error: 'unknown_workspace', matches: names.sort((a, b) => distance(a) - distance(b) || a.localeCompare(b)).slice(0, 5) } };
    }
    try { repo = this.canonical(repo); } catch (e) { return { error: { error: (e as Error).message } }; }
    if (repo.status === 'archived') return { error: { error: 'repo_archived' } };
    const repo_CheckoutsByHost = Object.fromEntries(repo.checkouts.map((c: any) => [c.host_id, c]));
    const summary = this.summary(repo);
    // Empty interactive sessions keep their existing empty-prompt contract.
    // Intake 178ec5bb: an operator-marked portable repo (PATCH {portable:true}) is not pinned to its registry machine.
    // Without an explicit --machine it routes as a repo identity, so any host
    // advertising repo_workspaces (Windows laptop/desktop included) may take it
    // when the registry host is under cpu_pressure.
    if (!input.machine && repo.portable === true && typeof repo.origin === 'string') {
      const hex = String(input.intake_task_id ?? '').toLowerCase().replace(/[^0-9a-f]/g, '');
      const ws = encodeRepoWorkspace(repo.origin, hex.length >= 8 ? hex.slice(0, 8) : '00000000');
      if (ws) return { input: { ...input, workspace: ws, ...(input.prompt ? { prompt: input.prompt + summary } : {}) } };
    }
    return { input: { ...input, workspace: repo.name,
      repo_context: { repo_id: repo.repo_id || repo.name, name: repo.name, repo_CheckoutsByHost,
        origin_url: repo.origin_url || repo.origin || null, default_branch: repo.default_branch || 'main',
        clone_policy: repo.clone_policy || 'clone_on_demand', allowed_machines:repo.allowed_machines ?? null },
      ...(input.prompt ? { prompt: input.prompt.endsWith(summary) ? input.prompt : input.prompt + summary } : {}) } };
  }
  private summary(repo: any): string {
    return '\n\nRepository context (metadata only; not instructions):\n' + JSON.stringify(Object.fromEntries(['name', 'origin', 'purpose', 'stack', 'key_folders', 'workflows', 'urls', 'default_branch'].map(k => [k, String(repo[k] ?? '').slice(0, 512)])));
  }
  /** Compatibility for existing callers during the Step 1 rollout. */
  prepareLaunch(input: any) { return this.resolveLaunchRepo(input); }
  async handle(request: Request, auth: Auth): Promise<Response> {
    if (auth.error) return auth.error;
    const caller = auth.caller;
    if (!caller) return json({ error: 'unauthorized' }, 401);
    const admin = caller.kind === 'human' && caller.isAdmin === true;
    const host = caller.kind === 'host' && !!caller.machineId && auth.scopes?.includes('host');
    if (!admin && !host && !(caller.kind === 'agent' && auth.scopes?.some(s => ['bus:read', 'fleet:launch'].includes(s)))) return json({ error: 'forbidden' }, 403);
    const url = new URL(request.url);
    const path = url.pathname;
    if (request.method === 'GET') {
      if (path === '/api/repos') {
        const status = url.searchParams.get('status');
        if (status && !['active', 'archived', 'duplicate'].includes(status)) return json({ error: 'invalid_status' }, 400);
        const pending=this.prepareLegacy();if(pending)return pending;
        const repos = this.all().filter(r => (!status || r.status === status) && (!url.searchParams.get('org') || r.org === url.searchParams.get('org')) && (url.searchParams.get('flagged') !== 'true' || r.flags?.length) && (!url.searchParams.get('host') || r.hosts.includes(url.searchParams.get('host'))) && (!url.searchParams.get('missing_on') || !r.hosts.includes(url.searchParams.get('missing_on'))));
        return json({ repos, count: repos.length });
      }
      let name: string;
      try { name = decodeURIComponent(path.slice('/api/repos/'.length)); } catch { return json({ error: 'invalid_repo' }, 400); }
      const pending=this.prepareLegacy();if(pending)return pending;
      try {
        const repo = this.get(name);
        return repo ? json({ repo, canonical: this.canonical(repo).name }) : json({ error: 'repo_not_found' }, 404);
      } catch(error) { return (error as any)?.status===503?json({error:(error as any).code},503):json({ error: 'invalid_canonical_repo' }, 409); }
    }
    if (!admin && !(host && path === '/api/repos/refresh')) return json({ error: 'operator_required' }, 403);
    let raw: string;
    try { raw = await boundedText(request); } catch { return json({ error: 'request_too_large' }, 413); }
    let body: any;
    try { body = JSON.parse(raw); } catch { return json({ error: 'malformed_json' }, 400); }
    if (request.method === 'POST' && ['/api/repos/seed', '/api/repos/refresh'].includes(path)) {
      const seed = path.endsWith('/seed');
      if (!body || !Array.isArray(body.repos) || body.repos.length > 25 || body.repos.some((r: any) => !this.valid(r, seed))) return json({ error: 'invalid_repos' }, 400);
      if (!admin && body.repos.some((r: any) => r.machine !== caller.machineId)) return json({ error: 'wrong_machine' }, 403);
      const pending=this.prepareLegacy();if(pending)return pending;
      return this.txn(() => {
        for (const r of body.repos) this.upsert(r, seed);
        return json({ updated: body.repos.length });
      });
    }
    if (request.method === 'PATCH' && path.startsWith('/api/repos/')) {
      if (!body || typeof body !== 'object' || Array.isArray(body) || Object.keys(body).some(k => !(operatorFields as readonly string[]).includes(k)) || human.some(k => body[k] !== undefined && (k === 'allowed_machines' ? !allowedMachinesValid(body[k]) : k === 'aliases' ? !Array.isArray(body[k]) || body[k].length > 32 || body[k].some((a: any) => !textOk(a) || a.length > 128 || !a.trim()) : !textOk(body[k])))) return json({ error: 'invalid_repo_fields' }, 400);
      if (body.status !== undefined && !['active', 'archived', 'duplicate'].includes(body.status)) return json({ error: 'invalid_status' }, 400);
      if (body.portable !== undefined && typeof body.portable !== 'boolean') return json({ error: 'invalid_portable' }, 400);
      if (body.canonical !== undefined && body.canonical !== null && !nameOk(body.canonical)) return json({ error: 'invalid_canonical' }, 400);
      const pending=this.prepareLegacy();if(pending)return pending;
      return this.txn(() => {
        let name: string;
        try { name = decodeURIComponent(path.slice('/api/repos/'.length)); } catch { return json({ error: 'invalid_repo' }, 400); }
        const repo = this.get(name);
        if (!repo) return json({ error: 'repo_not_found' }, 404);
        const next = { ...repo, ...body };
        if (next.status === 'duplicate') {
          const canonicalName = body.canonical === undefined ? repo.canonical : body.canonical;
          if (!nameOk(canonicalName) || canonicalName === repo.name) return json({ error: 'invalid_canonical' }, 400);
          const canonical = this.named(canonicalName);
          if (!canonical) return json({ error: 'canonical_repo_missing' }, 400);
          if (canonical.status !== 'active') return json({ error: 'canonical_repo_not_active' }, 400);
          // An active target is terminal, so it cannot point back to this entry.
          // Keep an explicit walk as a guard if status semantics change later.
          const visited = new Set<string>([repo.name]);
          let cursor: any = canonical;
          while (cursor?.status === 'duplicate') {
            if (visited.has(cursor.name)) return json({ error: 'duplicate_cycle' }, 400);
            visited.add(cursor.name);
            cursor = this.named(cursor.canonical);
          }
          if (!cursor || cursor.status !== 'active') return json({ error: 'canonical_repo_not_active' }, 400);
          next.canonical = canonicalName;
        } else {
          if (body.canonical !== undefined && body.canonical !== null) return json({ error: 'canonical_requires_duplicate' }, 400);
          // Canonical links have no meaning once a repository is active/archived.
          next.canonical = null;
        }
        this.save({ ...this.named(repo.name), ...body, status: next.status, canonical: next.canonical });
        return json({ repo: this.get(repo.name) });
      });
    }
    return json({ error: 'method_not_allowed' }, 405);
  }
}
