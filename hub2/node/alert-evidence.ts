// Optional Node-only authority for the credential monitor's EXISTING picker POST.
// No mail, provider call, native process, schema migration or automatic enrollment.
// Activation is a separate owner-verified boundary: create private canonical
// enrollment.json + journal.json only after the reviewed marked monitor is the
// maintained caller. Missing files are UNKNOWN, never an inferred zero. Only
// server decision invocations are witnessed here, not arbitrary cron lifecycle.
// release_sha is owner-pinned provenance, backed by load-time/current artifact
// hashes, not a cryptographic deployment attestation. Local/remote enrollment
// must separately pin the installed Hetzner monitor and authenticated VM origin.
// Every process birth changes the interval generation; no interval may cross a
// restart/offline gap. An invalid initialized sentinel is refused; old process
// snapshots are never used to claim continuity. At most128 active invocations
// are observed; cumulative safe-integer counts do not reset. First256 immutable
// snapshots are retained, then two atomic cumulative checkpoint slots roll
// forward under this birth only. No old-birth record is overwritten or deleted.
// Actual loss/error remains sticky UNKNOWN; an owned wait expiry alone does not
// abandon physical IO or stop accounting. All primary responses continue.
// journal.json remains the immutable initialized sentinel.
// Source owners must manage retention only outside an accepted interval.
// Diagnostic producers are separate opt-ins; the default credential_monitor
// enrollment/journal contract remains unchanged. PickerControl stays independent.
import { constants } from 'node:fs';
import { open, lstat, readdir, rename, link, unlink, type FileHandle } from 'node:fs/promises';
import { createHash, randomUUID } from 'node:crypto';
import { types as utilTypes } from 'node:util';
import { isAbsolute, resolve } from 'node:path';
import { fileURLToPath, URL } from 'node:url';
import type { CredentialMonitorEvidence, EvidencePersistStage, EvidencePersistError, JsonRecord, PickerDiagnosticProducer } from '../src/types.ts';

const LIMIT = 131072;
const MAX_ACTIVE = 128; // Bounded active identities, not a lifetime request ceiling.
const PREFIX_SNAPSHOTS = 256; // Existing immutable prefix is retained and verified.
const MAX_FILES = 8192; // Retention exhaustion is UNKNOWN; no automatic cleanup.
const RESERVED_FILES = PREFIX_SNAPSHOTS + 3; // Prefix + two slots + one temporary.
const TIMEOUT_MS = 150;
const RECIPIENT = 'dev@dtcdev.click';
type Producer = 'credential_monitor' | PickerDiagnosticProducer;
const PRODUCERS: readonly Producer[] = ['credential_monitor', 'picker_excluded', 'picker_quota_stale'];
const here = fileURLToPath(import.meta.url);
const requiredArtifacts = [here, fileURLToPath(new URL('./main.ts', import.meta.url)),
  fileURLToPath(new URL('../src/worker.ts', import.meta.url))];
const diagnosticArtifacts = [...requiredArtifacts, fileURLToPath(new URL('../src/model-picker.ts', import.meta.url)),
  fileURLToPath(new URL('../src/routing-pool.ts', import.meta.url))];
type Artifact = { path: string; sha256: string };
type Enrollment = { kind: string; producer: string; recipient: string; enrollment_id: string;
  source_id: string; release_sha: string; artifacts: Artifact[] };
type Journal = { kind: string; producer?: PickerDiagnosticProducer; source_id: string; enrollment_id: string;
  occurrence_count: number; started_total: number; finished_total: number };
// Test-only IO injection writes an already O_EXCL-opened record; production
// always uses the default file + directory fsync implementation below.
type SnapshotWriter = (value: JsonRecord, handle: FileHandle) => Promise<void>;
export function canonical(value: unknown): string {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value !== null && typeof value === 'object') return '{' + Object.keys(value).sort()
    .map(key => JSON.stringify(key) + ':' + canonical((value as JsonRecord)[key])).join(',') + '}';
  return JSON.stringify(value);
}
const hash = (value: string | Uint8Array) => createHash('sha256').update(value).digest('hex');
const integer = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;
function check(ok: unknown): asserts ok { if (!ok) throw new Error('evidence_unavailable'); }
function exact(value: object, keys: string[]) { check(Object.keys(value).sort().join(',') === [...keys].sort().join(',')); }

// Exhaustive type-checked tables are the only unavailable-field projection.
const PERSIST_STAGES: Record<EvidencePersistStage, true> = {
  artifacts:true,store_identity:true,store_sentinel:true,snapshot_create:true,
  snapshot_write:true,snapshot_sync:true,snapshot_close:true,ancestor_sync:true,
  checkpoint_verify:true,checkpoint_create:true,checkpoint_write:true,checkpoint_sync:true,
  checkpoint_close:true,checkpoint_publish:true,unspecified:true,
};
const PERSIST_ERRORS: Record<EvidencePersistError, true> = {
  deadline:true,permission:true,missing:true,collision:true,capacity:true,
  readonly:true,io:true,fd_limit:true,error:true,opaque:true,
};
const persistStages = new Set(Object.keys(PERSIST_STAGES));
const persistErrors = new Set(Object.keys(PERSIST_ERRORS));
export function persistDiagnosticFields(stage:unknown,error:unknown){
  return {unavailable_persist_stage:typeof stage==='string'&&persistStages.has(stage)?stage:'unspecified',
    unavailable_persist_error:typeof error==='string'&&persistErrors.has(error)?error:'opaque'};
}
// Closed per-slot progress only. No artifact paths, stat values or timestamps.
type ArtifactOperation = 'open' | 'stat' | 'read' | 'hash' | 'close';
type ArtifactProgress = {slots:Map<number,ArtifactOperation>;completed:number;phase:'validation'|'files'|'proof'|'complete'};
type ArtifactProgressSnapshot = {phase:'unspecified'}|{slots:{slot:number;operation:ArtifactOperation}[];completed:number;phase:'validation'|'files'|'proof'|'complete'};
const ARTIFACT_OPERATIONS = new Set<ArtifactOperation>(['open','stat','read','hash','close']);
function freezeArtifactProgress(progress:ArtifactProgress|undefined):ArtifactProgressSnapshot|undefined{
  if(!progress)return undefined;
  // Internal bounded state only. If it cannot be represented exactly, omit
  // individual slots rather than guessing which operation caused the failure.
  const rows=[...progress.slots];
  if(rows.length>2||!Number.isInteger(progress.completed)||progress.completed<0||progress.completed>8
    ||!['validation','files','proof','complete'].includes(progress.phase)
    ||rows.some(([slot,operation])=>!Number.isInteger(slot)||slot<0||slot>7||!ARTIFACT_OPERATIONS.has(operation)))
    return {phase:'unspecified'};
  return {slots:rows.sort((a,b)=>a[0]-b[0]).map(([slot,operation])=>({slot,operation})),completed:progress.completed,phase:progress.phase};
}
function artifactDiagnosticFields(progress:ArtifactProgressSnapshot|undefined){
  return progress?.phase==='unspecified'?{unavailable_artifact_phase:'unspecified'}:progress?{unavailable_artifact_slots:progress.slots.map(row=>({...row})),
    unavailable_artifact_completed:progress.completed,unavailable_artifact_phase:progress.phase}:{};
}
type PersistTimerLateness = 'on_tick'|'delayed'|'unspecified';
function persistClock():number|undefined{
  try{const value=performance.now();return Number.isFinite(value)&&value>=0?value:undefined;}catch{return undefined;}
}
type WaitBudget = {start:number;last:number;deadline:number;invalid:boolean};
function waitBudget(start:number|undefined,limit:number):WaitBudget {
  if(start===undefined||!Number.isFinite(start)||start<0||!Number.isFinite(limit)||limit<=0)
    throw new Error('observer_clock_unavailable');
  return {start,last:start,deadline:start+limit,invalid:false};
}
function budgetRead(budget:WaitBudget):number {
  const now=persistClock();
  if(budget.invalid||now===undefined||now<budget.start||now<budget.last){
    budget.invalid=true;throw new Error('observer_clock_invalid');
  }
  budget.last=now;return now;
}
// Diagnostic stage reads also cannot conceal a chronology reversal. They still
// freeze the original error; the owner refuses authority on an invalid budget.
function traceClock(trace:PersistTrace|undefined):number|undefined {
  const now=persistClock(),budget=trace?.waitBudget;
  if(budget){
    if(now===undefined||now<budget.start||now<budget.last)budget.invalid=true;
    else budget.last=now;
  }
  return now;
}
function persistTimerLateness(start:number|undefined,budget:number,chronology?:WaitBudget):PersistTimerLateness{
  const end=chronology?budgetRead(chronology):persistClock();
  if(start===undefined||end===undefined||end-start<budget)return 'unspecified';
  // Diagnostic tolerance only, NEVER a timeout extension or cause attribution.
  return end-start<=budget+50?'on_tick':'delayed';
}
type PersistAgeBucket = 'under_25'|'25_to_74'|'75_to_149'|'150_or_more'|'unspecified';
type PersistBudget = {before_stage:PersistAgeBucket;stage_await:PersistAgeBucket};
function persistAge(value:number|undefined):PersistAgeBucket{
  if(value===undefined||!Number.isFinite(value)||value<0)return 'unspecified';
  return value<25?'under_25':value<75?'25_to_74':value<150?'75_to_149':'150_or_more';
}
// Five completed pre-sync phases only; pending/failing work is never credited.
// Buckets describe JS boundaries, not kernel IO or an exact timer allocation.
const COMPLETED_PHASES = ['artifacts','store_identity','store_sentinel','snapshot_create','snapshot_write'] as const;
type CompletedPhase = typeof COMPLETED_PHASES[number];
type CompletedPhases = Partial<Record<CompletedPhase,PersistAgeBucket>>;
type CompletedPhasesSnapshot = CompletedPhases|'unspecified';
const COMPLETED_AGES = new Set(['under_25','25_to_74','75_to_149','150_or_more','unspecified']);
function freezeCompletedPhases(value:unknown):CompletedPhasesSnapshot|undefined{
  if(value===undefined)return undefined;
  try{
    if(value===null||typeof value!=='object'||utilTypes.isProxy(value)
      ||Object.getPrototypeOf(value)!==Object.prototype)return 'unspecified';
    const keys=Reflect.ownKeys(value);
    if(keys.length>COMPLETED_PHASES.length)return 'unspecified';
    const result:CompletedPhases={};
    for(const key of keys){
      if(typeof key!=='string'||!COMPLETED_PHASES.includes(key as CompletedPhase))return 'unspecified';
      const descriptor=Object.getOwnPropertyDescriptor(value,key);
      if(!descriptor||!('value' in descriptor)||typeof descriptor.value!=='string'
        ||!COMPLETED_AGES.has(descriptor.value))return 'unspecified';
      result[key as CompletedPhase]=descriptor.value as PersistAgeBucket;
    }
    return result;
  }catch{return 'unspecified';}
}
function completedPhaseFields(value:unknown){
  const frozen=freezeCompletedPhases(value);
  return frozen===undefined?{}:{unavailable_persist_completed:frozen};
}
function persistBudget(trace:PersistTrace):PersistBudget{
  const now=traceClock(trace),start=trace.budgetStart,stage=trace.stageStart;
  // These are JS operation-boundary observations, never kernel IO latency.
  // Invalid ordering cannot be projected as a short or successful operation.
  if(start===undefined||stage===undefined||now===undefined||stage<start||now<stage)
    return {before_stage:'unspecified',stage_await:'unspecified'};
  return {before_stage:persistAge(stage-start),stage_await:persistAge(now-stage)};
}
function persistBudgetFields(budget:PersistBudget|undefined){
  return budget?{unavailable_persist_before_stage:budget.before_stage,
    unavailable_persist_stage_await:budget.stage_await}:{};
}
function firstPersistFailure(trace:PersistTrace|undefined,stage:EvidencePersistStage,error:unknown,timer?:PersistTimerLateness){
  if(trace&&!trace.first)trace.first={stage,error,budget:persistBudget(trace),completed:freezeCompletedPhases(trace.completed),
    ...(stage==='artifacts'?{artifacts:freezeArtifactProgress(trace.artifacts)}:{}),...(timer?{timer}:{})};
  trace?.failed?.(error);
}
const ownDeadlines = new WeakSet<object>();
type PersistTrace = { waitBudget?:WaitBudget; failed?:(error:unknown)=>void; completed?:CompletedPhases; stage: EvidencePersistStage; budgetStart?:number;stageStart?:number;artifacts?:ArtifactProgress; first?: {stage:EvidencePersistStage;error:unknown;budget?:PersistBudget;completed?:CompletedPhasesSnapshot;artifacts?:ArtifactProgressSnapshot;timer?:PersistTimerLateness} };
async function persistStep<T>(trace:PersistTrace|undefined,stage:EvidencePersistStage,work:()=>Promise<T>):Promise<T>{
  if(trace){trace.stage=stage;trace.stageStart=traceClock(trace);}
  const started=trace?.stageStart;
  try{
    const result=await work();
    if(trace&&COMPLETED_PHASES.includes(stage as CompletedPhase)){
      const ended=traceClock(trace);
      (trace.completed??={})[stage as CompletedPhase]=persistAge(started===undefined||ended===undefined?undefined:ended-started);
    }
    return result;
  }
  catch(error){firstPersistFailure(trace,stage,error);throw error;}
}
export function classifyPersistFailure(error:unknown):EvidencePersistError{
  try{
    if(error!==null&&(typeof error==='object'||typeof error==='function')){
      if(ownDeadlines.has(error))return 'deadline';
      // A Proxy descriptor/getPrototypeOf trap is user code. Do not invoke it,
      // access .code/.message, stringify, or inspect arbitrary class names.
      if(utilTypes.isProxy(error)||!utilTypes.isNativeError(error))return 'opaque';
      const descriptor=Object.getOwnPropertyDescriptor(error,'code');
      const code=descriptor&&'value' in descriptor?descriptor.value:undefined;
      if(typeof code==='string'){
        if(code==='EACCES'||code==='EPERM')return 'permission';
        if(code==='ENOENT')return 'missing';if(code==='EEXIST')return 'collision';
        if(code==='ENOSPC'||code==='EDQUOT')return 'capacity';if(code==='EROFS')return 'readonly';
        if(code==='EIO')return 'io';if(code==='EMFILE'||code==='ENFILE')return 'fd_limit';
      }
      return 'error';
    }
  }catch{} // Failed introspection never becomes a second failure or raw payload.
  return 'opaque';
}

class PrivateStore {
  readonly handles: FileHandle[] = [];
  private journalBaseline: string | undefined;
  readonly boot = randomUUID().replaceAll('-', '');
  private commits: { name: string; bytes: string; digest: string; journal: string }[] = [];
  private checkpoints: { name: string; bytes: string; digest: string; journal: string; sequence: number }[] = [];
  private constructor(readonly path: string) {}
  static async load(path: string) {
    check(process.platform === 'linux' && isAbsolute(path) && resolve(path) === path);
    const store = new PrivateStore(path);
    try {
      let handle = await open('/', constants.O_RDONLY | constants.O_DIRECTORY);
      store.handles.push(handle);
      for (const part of path.split('/').filter(Boolean)) {
        handle = await open(`/proc/self/fd/${handle.fd}/${part}`,
          constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
        store.handles.push(handle);
        const st = await handle.stat();
        check([0, process.geteuid!()].includes(st.uid) && !(st.mode & 0o002));
      }
      const last = await handle.stat();
      check(last.uid === process.geteuid!() && !(last.mode & 0o077));
      // Reserve this birth's full finite namespace. Old records/partial writes
      // are counted and retained; no restart can silently evict them.
      check((await readdir(store.name('.'))).length <= MAX_FILES - RESERVED_FILES);
      return store;
    } catch { await store.close(); throw new Error('evidence_unavailable'); }
  }
  get directory() { return this.handles[this.handles.length - 1]; }
  name(name: string) { return `/proc/self/fd/${this.directory.fd}/${name}`; }
  async identity(trace?:PersistTrace) {
    const failure=(error:unknown):never=>{
      firstPersistFailure(trace,trace?.stage??'store_identity',error);throw error;
    };
    const rows=await Promise.allSettled([lstat(this.path).catch(failure),this.directory.stat().catch(failure)]);
    if(rows[0].status==='rejected')throw rows[0].reason;
    if(rows[1].status==='rejected')throw rows[1].reason;
    const actual=rows[0].value,pinned=rows[1].value;
    check(actual.isDirectory() && actual.ino === pinned.ino && actual.dev === pinned.dev
      && actual.uid === process.geteuid!() && !(actual.mode & 0o077));
  }
  async read(name: string) {
    const handle = await open(this.name(name), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    try {
      const st = await handle.stat();
      check(st.isFile() && st.uid === process.geteuid!() && !(st.mode & 0o077) && st.nlink === 1 && st.size <= LIMIT);
      const buffer = Buffer.alloc(LIMIT + 1);
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0);
      check(bytesRead <= LIMIT && bytesRead === st.size);
      const raw = buffer.subarray(0, bytesRead).toString('utf8');
      const value = JSON.parse(raw);
      // A canonical private format also refuses duplicate JSON keys.
      check(value && typeof value === 'object' && !Array.isArray(value) && raw.trim() === canonical(value));
      return value;
    } finally { await handle.close(); }
  }
  bindJournal(value: Journal) {
    check(this.journalBaseline === undefined);
    this.journalBaseline = canonical(value);
  }
  async write(value: Journal, writer?: SnapshotWriter, trace?: PersistTrace) {
    await persistStep(trace,'store_identity',()=>this.identity(trace));
    await persistStep(trace,'store_sentinel',async()=>check(this.journalBaseline !== undefined
      && canonical(await this.read('journal.json')) === this.journalBaseline));
    if (this.commits.length === PREFIX_SNAPSHOTS) {
      await this.checkpoint(value, writer, trace);
      return;
    }
    const sequence = this.commits.length + 1;
    const name = `snapshot-${this.boot}-${sequence}.json`;
    const previous = this.commits.at(-1)?.digest ?? hash(this.journalBaseline!);
    const record = {kind: value.kind === 'hub_credential_monitor_journal_v1'
      ? 'hub_credential_monitor_snapshot_v1' : 'hub_picker_diagnostic_snapshot_v1', controller_id: this.boot,
      sequence, previous_sha256: previous, journal: value};
    const bytes = canonical(record);
    const handle = await persistStep(trace,'snapshot_create',()=>open(this.name(name),
      constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600));
    try {
      if (writer) await persistStep(trace,'snapshot_write',()=>writer(record, handle));
      else {
        await persistStep(trace,'snapshot_write',()=>handle.writeFile(bytes + '\n', 'utf8'));
        await persistStep(trace,'snapshot_sync',()=>handle.sync());
      }
    } finally { await persistStep(trace,'snapshot_close',()=>handle.close()); }
    if (!writer) for (const directory of [...this.handles].reverse()) await persistStep(trace,'ancestor_sync',()=>directory.sync());
    // No current pointer, rename, overwrite or eviction. Even if a later append
    // succeeds, every older record and the initialized sentinel remain required
    // evidence. A failed partial file is retained; this generation stays dirty.
    this.commits.push({name, bytes, digest: hash(bytes), journal: canonical(value)});
  }
  private async checkpoint(value: Journal, writer?: SnapshotWriter, trace?: PersistTrace) {
    // Only this process's two slots may roll forward. The immutable prefix and
    // all prior births remain untouched. Verify BOTH slots before replacement,
    // so a corrupt superseded slot cannot be silently healed by the next write.
    await persistStep(trace,'checkpoint_verify',async()=>{
      for (const record of this.checkpoints)
        check(canonical(await this.read(record.name)) === record.bytes);
    });
    const latest = this.checkpoints.at(-1) ?? this.commits.at(-1)!;
    const previousSequence = this.checkpoints.at(-1)?.sequence ?? PREFIX_SNAPSHOTS;
    check(previousSequence < Number.MAX_SAFE_INTEGER);
    const sequence = previousSequence + 1;
    const name = `checkpoint-${this.boot}-${sequence % 2}.json`;
    const previous = this.checkpoints.find(record => record.name === name);
    const record = {kind: value.kind === 'hub_credential_monitor_journal_v1'
      ? 'hub_credential_monitor_checkpoint_v1' : 'hub_picker_diagnostic_checkpoint_v1',
      controller_id: this.boot, sequence, prefix_sha256: this.commits.at(-1)!.digest,
      previous_sha256: latest.digest, journal: value};
    const bytes = canonical(record), temporary = `.checkpoint-${this.boot}-${sequence}.tmp`;
    const handle = await persistStep(trace,'checkpoint_create',()=>open(this.name(temporary),
      constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600));
    try {
      if (writer) await persistStep(trace,'checkpoint_write',()=>writer(record, handle));
      else { await persistStep(trace,'checkpoint_write',()=>handle.writeFile(bytes + '\n', 'utf8'));
        await persistStep(trace,'checkpoint_sync',()=>handle.sync()); }
    } finally { await persistStep(trace,'checkpoint_close',()=>handle.close()); }
    await persistStep(trace,'checkpoint_publish',async()=>{
    await this.identity(trace);
    if (previous) {
      check(canonical(await this.read(name)) === previous.bytes);
      await rename(this.name(temporary), this.name(name));
    } else {
      // link is exclusive: a raced/pre-existing first-slot name is never replaced.
      // A crash between link/unlink leaves nlink2 and is UNKNOWN, not repaired.
      await link(this.name(temporary), this.name(name));
      await unlink(this.name(temporary));
    }
    });
    if (!writer) for (const directory of [...this.handles].reverse()) await persistStep(trace,'ancestor_sync',()=>directory.sync());
    // Admission only AFTER durable publication. Any error dirties the observer;
    // partial own files remain for owner diagnosis, never automatic recovery.
    this.checkpoints = this.checkpoints.filter(item => item.name !== name);
    this.checkpoints.push({name, bytes, digest: hash(bytes), journal: canonical(value), sequence});
  }
  async verify(trace?:PersistTrace): Promise<string> {
    await this.identity(trace);
    check(this.journalBaseline !== undefined
      && canonical(await this.read('journal.json')) === this.journalBaseline);
    for (const record of this.commits)
      check(canonical(await this.read(record.name)) === record.bytes);
    for (const record of this.checkpoints)
      check(canonical(await this.read(record.name)) === record.bytes);
    // The two retained checkpoints are adjacent and hash-linked; the first
    // rollup's predecessor is the still-retained immutable prefix tail.
    if (this.checkpoints.length === 1)
      check(JSON.parse(this.checkpoints[0].bytes).previous_sha256 === this.commits.at(-1)?.digest);
    if (this.checkpoints.length === 2)
      check(this.checkpoints[1].sequence === this.checkpoints[0].sequence + 1
        && JSON.parse(this.checkpoints[1].bytes).previous_sha256 === this.checkpoints[0].digest);
    return this.checkpoints.at(-1)?.journal ?? this.commits.at(-1)?.journal ?? this.journalBaseline;
  }
  async close() {
    const rows=await Promise.allSettled(this.handles.splice(0).map(handle=>handle.close()));
    const failed=rows.find((row):row is PromiseRejectedResult=>row.status==='rejected');
    if(failed)throw failed.reason;
  }
}

async function verifyArtifacts(entry: Enrollment, loaded: Record<string, string>, trace?:PersistTrace) {
  const progress:ArtifactProgress={slots:new Map(),completed:0,phase:'validation'};
  if(trace)trace.artifacts=progress;
  check(Array.isArray(entry.artifacts) && entry.artifacts.length >= 3 && entry.artifacts.length <= 8);
  const paths = new Set<string>();
  for (const artifact of entry.artifacts) {
    exact(artifact, ['path', 'sha256']);
    check(typeof artifact.path === 'string' && isAbsolute(artifact.path) && !paths.has(artifact.path)
      && /^[0-9a-f]{64}$/.test(artifact.sha256));
    paths.add(artifact.path);
  }
  // Fresh descriptor-backed verification on EVERY invocation, never a cached or
  // shared proof. Two files at a time avoid five avoidable serial IO chains,
  // while bounding simultaneous handles/buffers (at most two 16MiB buffers).
  // Fail fast on an observed rejection; every started task retains its own
  // finally-close even after rejection/timeout. No next batch follows a verifier
  // rejection; an OUTER timeout still does not cancel underlying IO, as before.
  progress.phase='files';
  for (let index = 0; index < entry.artifacts.length; index += 2) {
    const batch = await Promise.allSettled(entry.artifacts.slice(index, index + 2).map(async (artifact,offset) => {
    const slot=index+offset;
    let handle:FileHandle|undefined;
    progress.slots.set(slot,'open');
    try {
      handle = await open(artifact.path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
      progress.slots.set(slot,'stat');
      const st = await handle.stat();
      check(st.isFile() && [0, process.geteuid!()].includes(st.uid) && !(st.mode & 0o022)
        && st.nlink === 1 && st.size <= 16 * 1024 * 1024);
      const buffer = Buffer.alloc(st.size + 1);
      progress.slots.set(slot,'read');
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0);
      check(bytesRead === st.size);
      progress.slots.set(slot,'hash');
      check(hash(buffer.subarray(0, bytesRead)) === artifact.sha256);
    } catch(error) { firstPersistFailure(trace,'artifacts',error);throw error; }
    finally {
      if(handle){
        progress.slots.set(slot,'close');
        try{await handle.close();}
        catch(error){firstPersistFailure(trace,'artifacts',error);throw error;}
      }
      progress.slots.delete(slot);
    }
    progress.completed++;
    }));
    // Rejection is latched inside each task immediately. Retain ownership until
    // EVERY started sibling/finally has settled; never start a later batch.
    const failed = batch.find((value):value is PromiseRejectedResult=>value.status==='rejected');
    if(failed)throw failed.reason;
  }
  progress.phase='proof';
  const required = entry.producer === 'credential_monitor' ? requiredArtifacts : diagnosticArtifacts;
  if (entry.producer !== 'credential_monitor') check(entry.artifacts.length === required.length);
  check(required.every(path => paths.has(path) && loaded[path] === entry.artifacts.find(item => item.path === path)?.sha256)
    && hash(canonical(entry.artifacts)) === entry.source_id);
  progress.phase='complete';
}

const FAILURE_REASONS = new Set(['invalidated', 'persist_failed', 'capacity_or_counter_limit',
  'finish_identity_invalid', 'snapshot_source_verification_failed', 'snapshot_store_verification_failed', 'closed',
  'picker_snapshot_failed', 'picker_pool_failed', 'picker_decision_incomplete',
  'picker_snapshot_no_host', 'picker_pool_no_host', 'picker_snapshot_data_unavailable',
  'picker_snapshot_policy_unavailable', 'picker_pool_policy_unavailable',
  'picker_begin_failed', 'picker_finish_failed', 'snapshot_route_timeout', 'snapshot_route_rejected',
  'dry_run_begin_failed', 'dry_run_finish_timeout', 'dry_run_finish_failed']);

/** One live birth: delayed SUCCESS can catch up; actual coverage loss cannot heal. */
export class AlertEvidence implements CredentialMonitorEvidence {
  readonly boot: string;
  private dirty = false;
  private firstFailure: string | undefined;
  private firstPersist: {stage:EvidencePersistStage;error:EvidencePersistError;budget?:PersistBudget;completed?:CompletedPhasesSnapshot;artifacts?:ArtifactProgressSnapshot;timer?:PersistTimerLateness} | undefined;
  private pending = 0;
  private active = new Set<number>();
  private requests = 0;
  private queue: Promise<void> = Promise.resolve();
  private nextSnapshot: {journal:Journal;revision:number} | undefined;
  private revision = 0;
  private durableRevision = 0;
  private physical: symbol | undefined;
  private responseWait: Promise<void> | undefined;
  private closing: Promise<void> | undefined;
  private constructor(private store: PrivateStore, readonly enrollment: Enrollment, private journal: Journal,
    private writer: SnapshotWriter | undefined, private readonly timeoutMs: number,
    private readonly loaded: Record<string, string>) { this.boot = store.boot; }

  static async load(directory: string, options: { loadedArtifacts: Record<string, string>; producer?: Producer; writer?: SnapshotWriter; timeoutMs?: number }) {
    const producer = options.producer ?? 'credential_monitor';
    check(PRODUCERS.includes(producer));
    const diagnostic = producer !== 'credential_monitor';
    const store = await PrivateStore.load(directory);
    try {
      const entry = await store.read('enrollment.json') as Enrollment;
      exact(entry, ['kind', 'producer', 'recipient', 'enrollment_id', 'source_id', 'release_sha', 'artifacts']);
      check(entry.kind === (diagnostic ? 'hub_picker_diagnostic_enrollment_v1' : 'hub_credential_monitor_enrollment_v1') && entry.producer === producer
        && entry.recipient === RECIPIENT && /^[0-9a-f]{32}$/.test(entry.enrollment_id)
        && /^[0-9a-f]{64}$/.test(entry.source_id) && /^[0-9a-f]{40}$/.test(entry.release_sha));
      await verifyArtifacts(entry, options.loadedArtifacts);
      const journal = await store.read('journal.json') as Journal;
      exact(journal, ['kind', 'source_id', 'enrollment_id', 'occurrence_count', 'started_total', 'finished_total', ...(diagnostic ? ['producer'] : [])]);
      check(journal.kind === (diagnostic ? 'hub_picker_diagnostic_journal_v1' : 'hub_credential_monitor_journal_v1')
        && (!diagnostic || journal.producer === producer) && journal.source_id === entry.source_id
        && journal.enrollment_id === entry.enrollment_id && integer(journal.occurrence_count)
        && integer(journal.started_total) && integer(journal.finished_total)
        && journal.occurrence_count <= journal.finished_total && journal.finished_total === journal.started_total);
      store.bindJournal(journal);
      return new AlertEvidence(store, entry, journal, options.writer, options.timeoutMs ?? TIMEOUT_MS, Object.freeze({ ...options.loadedArtifacts }));
    } catch { await store.close(); throw new Error('evidence_unavailable'); }
  }
  invalidate(reason: unknown = 'invalidated') {
    // Keep only a closed first-fault enum; caller errors/paths never become wire data.
    if (this.firstFailure === undefined) this.firstFailure = typeof reason === 'string' && FAILURE_REASONS.has(reason) ? reason : 'invalidated';
    this.dirty = true;
  }
  private rememberPersist(trace:PersistTrace,error:unknown) {
    if(this.firstFailure!==undefined&&this.firstFailure!=='persist_failed'){
      if(!this.ownDeadline(error))this.invalidate('persist_failed');
      return;
    }
    if(!this.firstPersist){
      const first=trace.first??{stage:trace.stage,error};
      this.firstPersist={stage:first.stage,error:classifyPersistFailure(first.error),...('budget' in first?{budget:first.budget}:{}),...('completed' in first?{completed:first.completed}:{}),...('artifacts' in first?{artifacts:first.artifacts}:{}),...('timer' in first?{timer:first.timer}:{})};
    }
    this.firstFailure??='persist_failed';
    if(!this.ownDeadline(error))this.invalidate('persist_failed');
  }
  private ownDeadline(error:unknown) {
    return error!==null&&typeof error==='object'&&ownDeadlines.has(error);
  }
  private async bounded<T>(work:Promise<T>,trace?:PersistTrace,start=persistClock(),provided?:WaitBudget):Promise<T> {
    // Own BOTH outcomes before any clock/deadline branch. Early refusal must
    // not abandon an already-started cleanup or its original tracked rejection.
    const observed=work.then(value=>({ok:true as const,value}),error=>({ok:false as const,error}));
    const budget=provided??waitBudget(start,this.timeoutMs);
    if(trace){trace.waitBudget=budget;if(trace.budgetStart===undefined)trace.budgetStart=start;}
    let timer:ReturnType<typeof setTimeout>|undefined;
    const expired=()=>{
      budgetRead(budget); // Invalid chronology is NOT an observed own deadline.
      const error=new Error('observer_timeout');ownDeadlines.add(error);
      if(trace)firstPersistFailure(trace,trace.stage,error,persistTimerLateness(start,this.timeoutMs,budget));
      if(budget.invalid)throw new Error('observer_clock_invalid');
      return error;
    };
    try {
      const remaining=Math.min(this.timeoutMs,budget.deadline-budgetRead(budget));
      if(remaining<=0)throw expired();
      const result=await Promise.race([observed,new Promise<never>((_,reject)=>{
        timer=setTimeout(()=>{try{reject(expired());}catch(error){reject(error);}},remaining);
      })]);
      if(!result.ok)throw result.error;
      if(budgetRead(budget)>=budget.deadline)throw expired();
      return result.value;
    } finally {if(timer!==undefined)clearTimeout(timer);}
  }
  private persist() {
    if(this.dirty)return;
    this.nextSnapshot={journal:structuredClone(this.journal),revision:this.revision};
    this.pump();
  }
  private pump() {
    if(this.dirty||this.physical||!this.nextSnapshot)return;
    const token=Symbol('publication');this.physical=token;this.pending=1;
    const start=persistClock();
    const trace:PersistTrace={stage:'artifacts',budgetStart:start,...(start===undefined?{}:{waitBudget:waitBudget(start,this.timeoutMs)})};
    trace.failed=error=>this.rememberPersist(trace,error);
    // queue is the ACTUAL physical lifetime. responseWait is a separate shared
    // bounded waiter; no per-callback queue or renewed coalesced-write budget.
    this.queue=(async()=>{
      try {
        while(this.nextSnapshot&&!this.dirty){
          const captured=this.nextSnapshot;this.nextSnapshot=undefined;
          trace.budgetStart=traceClock(trace);trace.completed={};
          await persistStep(trace,'artifacts',()=>verifyArtifacts(this.enrollment,this.loaded,trace));
          if(trace.waitBudget?.invalid)throw new Error('observer_clock_invalid');
          if(this.dirty)break;
          await this.store.write(captured.journal,this.writer,trace);
          check(this.physical===token);
          if(trace.waitBudget?.invalid)throw new Error('observer_clock_invalid');
          if(!this.dirty)this.durableRevision=captured.revision;
        }
      }catch(error){firstPersistFailure(trace,trace.stage,error);}
      finally {
        // This finally is reached only after ALL physical tasks/finalizers.
        if(this.physical!==token)this.invalidate('persist_failed');
        else {this.physical=undefined;this.pending=0;}
      }
    })();
    this.responseWait=this.bounded(this.queue,trace,start,trace.waitBudget).catch(error=>{
      if(!trace.first)firstPersistFailure(trace,trace.stage,error);
      else this.rememberPersist(trace,error);
    });
  }
  begin(): unknown {
    if(this.dirty||this.active.size>=MAX_ACTIVE||this.journal.started_total>=Number.MAX_SAFE_INTEGER
      ||this.requests>=Number.MAX_SAFE_INTEGER||this.revision>=Number.MAX_SAFE_INTEGER){
      this.invalidate('capacity_or_counter_limit');return null;
    }
    const identity=++this.requests;this.journal.started_total++;this.revision++;
    this.active.add(identity);this.persist();return identity;
  }
  finish(identity:unknown,decision:'alert'|'observation'|null):Promise<void> {
    if(this.dirty)return Promise.resolve();
    if(typeof identity!=='number'||!Number.isSafeInteger(identity)||!this.active.has(identity)
      ||(decision!=='alert'&&decision!=='observation')){
      this.invalidate('finish_identity_invalid');return Promise.resolve();
    }
    if(this.revision>=Number.MAX_SAFE_INTEGER||this.journal.finished_total>=Number.MAX_SAFE_INTEGER
      ||decision==='alert'&&this.journal.occurrence_count>=Number.MAX_SAFE_INTEGER){
      this.invalidate('capacity_or_counter_limit');return Promise.resolve();
    }
    this.active.delete(identity);this.journal.finished_total++;this.revision++;
    if(decision==='alert')this.journal.occurrence_count++;
    this.persist();return this.responseWait??Promise.resolve();
  }
  private unavailable(nonce:string):JsonRecord {
    return {kind:'hub_alert_evidence_unavailable_v1',available:false,reason:'coverage_unknown',nonce,
      unavailable_phase:'runtime',unavailable_reason:this.firstFailure??
        (this.pending?'pending_write':this.active.size?'active_invocation':'snapshot_raced'),
      ...(this.firstFailure==='persist_failed'?{...persistDiagnosticFields(this.firstPersist?.stage,this.firstPersist?.error),...persistBudgetFields(this.firstPersist?.budget),...completedPhaseFields(this.firstPersist?.completed),...artifactDiagnosticFields(this.firstPersist?.artifacts),...(this.firstPersist?.timer?{unavailable_persist_timer:this.firstPersist.timer}:{})}:{})};
  }
  async snapshot(nonce:string):Promise<JsonRecord> {
    if(this.dirty||this.physical||this.active.size||this.nextSnapshot||this.revision!==this.durableRevision)
      return this.unavailable(nonce);
    const start=persistClock();
    if(start===undefined){this.invalidate('snapshot_source_verification_failed');return this.unavailable(nonce);}
    const budget=waitBudget(start,this.timeoutMs);
    const token=Symbol('proof'),revision=this.revision,version=canonical(this.journal);
    const captured=structuredClone(this.journal),binding=canonical(this.enrollment);
    this.physical=token;this.pending=1;
    let storePhase=false;
    const trace:PersistTrace={stage:'artifacts',budgetStart:start,waitBudget:budget};
    trace.failed=error=>{
      if(this.ownDeadline(error))this.rememberPersist(trace,error);
      else this.invalidate(storePhase?'snapshot_store_verification_failed':'snapshot_source_verification_failed');
    };
    const job=(async()=>{
      try {
        await this.store.identity(trace);budgetRead(budget);
        check(canonical(await this.store.read('enrollment.json'))===binding);budgetRead(budget);
        await verifyArtifacts(this.enrollment,this.loaded,trace);budgetRead(budget);
        if(this.dirty)return this.unavailable(nonce);
        storePhase=true;
        const stored=await this.store.verify(trace);budgetRead(budget);
        if(this.revision!==revision||this.active.size||this.nextSnapshot)return this.unavailable(nonce);
        check(stored===version);
        const now=budgetRead(budget);
        if(this.dirty||this.physical!==token||this.revision!==revision||this.durableRevision!==revision
          ||this.active.size||this.nextSnapshot||canonical(this.enrollment)!==binding
          ||now===undefined||now>=start+this.timeoutMs)return this.unavailable(nonce);
        // Linearization point: construct all fields from the same captured
        // revision with no await until construction is complete.
        return {kind:'hub_controller_coverage_v1',producer:this.enrollment.producer,recipient:RECIPIENT,
          source_id:this.enrollment.source_id,release_sha:this.enrollment.release_sha,artifact_proof:'loaded_and_current_v1',
          epoch:hash(this.enrollment.enrollment_id+':'+this.boot),controller_id:this.boot,
          limitations:[],complete:true,started_total:captured.started_total,finished_total:captured.finished_total,
          baseline_count:0,baseline_invocations:0,occurrence_count:captured.occurrence_count,observed_at_ms:Date.now(),nonce};
      }catch(error){firstPersistFailure(trace,trace.stage,error);return this.unavailable(nonce);}
      finally {
        if(this.physical!==token)this.invalidate('snapshot_store_verification_failed');
        else {this.physical=undefined;this.pending=0;}
        this.pump();
      }
    })();
    this.queue=job.then(()=>{},()=>{this.invalidate('snapshot_store_verification_failed');}); // Catch finalizer failures too.
    try{return await this.bounded(job,trace,start,budget);}
    catch(error){if(!this.ownDeadline(error))this.invalidate('snapshot_source_verification_failed');return this.unavailable(nonce);}
  }
  async close() {
    this.invalidate('closed');
    // Never close directory FDs while a borrowed operation/finalizer owns them.
    this.closing??=this.queue.then(()=>this.store.close()).catch(error=>{this.invalidate('closed');throw error;});
    try{await this.bounded(this.closing);}
    catch{throw new Error('observer_cleanup_pending');}
  }

}
