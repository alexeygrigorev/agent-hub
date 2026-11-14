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
// Capacity/error remains sticky UNKNOWN and all primary responses continue.
// journal.json remains the immutable initialized sentinel.
// Source owners must manage retention only outside an accepted interval.
// Diagnostic producers are separate opt-ins; the default credential_monitor
// enrollment/journal contract remains unchanged. PickerControl stays independent.
import { constants } from 'node:fs';
import { open, lstat, readdir, rename, link, unlink, type FileHandle } from 'node:fs/promises';
import { createHash, randomUUID } from 'node:crypto';
import { isAbsolute, resolve } from 'node:path';
import { fileURLToPath, URL } from 'node:url';
import type { CredentialMonitorEvidence, JsonRecord, PickerDiagnosticProducer } from '../src/types.ts';

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
  async identity() {
    const [actual, pinned] = await Promise.all([lstat(this.path), this.directory.stat()]);
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
  async write(value: Journal, writer?: SnapshotWriter) {
    await this.identity();
    check(this.journalBaseline !== undefined
      && canonical(await this.read('journal.json')) === this.journalBaseline);
    if (this.commits.length === PREFIX_SNAPSHOTS) {
      await this.checkpoint(value, writer);
      return;
    }
    const sequence = this.commits.length + 1;
    const name = `snapshot-${this.boot}-${sequence}.json`;
    const previous = this.commits.at(-1)?.digest ?? hash(this.journalBaseline);
    const record = {kind: value.kind === 'hub_credential_monitor_journal_v1'
      ? 'hub_credential_monitor_snapshot_v1' : 'hub_picker_diagnostic_snapshot_v1', controller_id: this.boot,
      sequence, previous_sha256: previous, journal: value};
    const bytes = canonical(record);
    const handle = await open(this.name(name),
      constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600);
    try {
      if (writer) await writer(record, handle);
      else {
        await handle.writeFile(bytes + '\n', 'utf8');
        await handle.sync();
      }
    } finally { await handle.close(); }
    if (!writer) for (const directory of [...this.handles].reverse()) await directory.sync();
    // No current pointer, rename, overwrite or eviction. Even if a later append
    // succeeds, every older record and the initialized sentinel remain required
    // evidence. A failed partial file is retained; this generation stays dirty.
    this.commits.push({name, bytes, digest: hash(bytes), journal: canonical(value)});
  }
  private async checkpoint(value: Journal, writer?: SnapshotWriter) {
    // Only this process's two slots may roll forward. The immutable prefix and
    // all prior births remain untouched. Verify BOTH slots before replacement,
    // so a corrupt superseded slot cannot be silently healed by the next write.
    for (const record of this.checkpoints)
      check(canonical(await this.read(record.name)) === record.bytes);
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
    const handle = await open(this.name(temporary),
      constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | constants.O_NOFOLLOW, 0o600);
    try {
      if (writer) await writer(record, handle);
      else { await handle.writeFile(bytes + '\n', 'utf8'); await handle.sync(); }
    } finally { await handle.close(); }
    await this.identity();
    if (previous) {
      check(canonical(await this.read(name)) === previous.bytes);
      await rename(this.name(temporary), this.name(name));
    } else {
      // link is exclusive: a raced/pre-existing first-slot name is never replaced.
      // A crash between link/unlink leaves nlink2 and is UNKNOWN, not repaired.
      await link(this.name(temporary), this.name(name));
      await unlink(this.name(temporary));
    }
    if (!writer) for (const directory of [...this.handles].reverse()) await directory.sync();
    // Admission only AFTER durable publication. Any error dirties the observer;
    // partial own files remain for owner diagnosis, never automatic recovery.
    this.checkpoints = this.checkpoints.filter(item => item.name !== name);
    this.checkpoints.push({name, bytes, digest: hash(bytes), journal: canonical(value), sequence});
  }
  async verify(): Promise<string> {
    await this.identity();
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
  async close() { await Promise.all(this.handles.splice(0).map(handle => handle.close())); }
}

async function verifyArtifacts(entry: Enrollment, loaded: Record<string, string>) {
  check(Array.isArray(entry.artifacts) && entry.artifacts.length >= 3 && entry.artifacts.length <= 8);
  const paths = new Set<string>();
  for (const artifact of entry.artifacts) {
    exact(artifact, ['path', 'sha256']);
    check(typeof artifact.path === 'string' && isAbsolute(artifact.path) && !paths.has(artifact.path)
      && /^[0-9a-f]{64}$/.test(artifact.sha256));
    paths.add(artifact.path);
    const handle = await open(artifact.path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    try {
      const st = await handle.stat();
      check(st.isFile() && [0, process.geteuid!()].includes(st.uid) && !(st.mode & 0o022)
        && st.nlink === 1 && st.size <= 16 * 1024 * 1024);
      const buffer = Buffer.alloc(st.size + 1);
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0);
      check(bytesRead === st.size && hash(buffer.subarray(0, bytesRead)) === artifact.sha256);
    } finally { await handle.close(); }
  }
  const required = entry.producer === 'credential_monitor' ? requiredArtifacts : diagnosticArtifacts;
  if (entry.producer !== 'credential_monitor') check(entry.artifacts.length === required.length);
  check(required.every(path => paths.has(path) && loaded[path] === entry.artifacts.find(item => item.path === path)?.sha256)
    && hash(canonical(entry.artifacts)) === entry.source_id);
}

/** One live Node incarnation. No clean sample is possible after any observer error. */
export class AlertEvidence implements CredentialMonitorEvidence {
  readonly boot: string;
  private dirty = false;
  private pending = 0;
  private active = new Set<number>();
  private requests = 0;
  private queue: Promise<void> = Promise.resolve();
  private nextSnapshot: Journal | undefined;
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
  invalidate() { this.dirty = true; }
  private async bounded(work: Promise<void>) {
    let timer: ReturnType<typeof setTimeout> | undefined;
    try { await Promise.race([work, new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error('observer_timeout')), this.timeoutMs);
    })]); }
    finally { clearTimeout(timer); }
  }
  private persist() {
    if (this.dirty) return;
    // This is a cumulative journal, not an event queue. Coalesce only pending
    // snapshots; every counter/active request remains represented in the latest
    // state. At most one physical write plus one latest snapshot is retained.
    this.nextSnapshot = structuredClone(this.journal);
    if (this.pending) return;
    this.pending = 1;
    this.queue = (async () => {
      try {
        while (this.nextSnapshot && !this.dirty) {
          const value = this.nextSnapshot;
          this.nextSnapshot = undefined;
          await this.bounded((async () => {
            await verifyArtifacts(this.enrollment, this.loaded);
            await this.store.write(value, this.writer);
          })());
        }
      } catch { this.invalidate(); }
      finally {
        // Clear the busy flag before this async body resolves. A separate
        // promise.finally would leave a microtask window that could drop a new
        // cumulative snapshot enqueued after the loop's last condition check.
        this.pending = 0;
        this.nextSnapshot = undefined;
      }
    })();
  }
  begin(): unknown {
    if (this.dirty || this.active.size >= MAX_ACTIVE || this.journal.started_total >= Number.MAX_SAFE_INTEGER) {
      this.invalidate(); return null;
    }
    const identity = ++this.requests;
    this.journal.started_total++;
    this.active.add(identity);
    this.persist();
    return identity;
  }
  async finish(identity: unknown, decision: 'alert' | 'observation' | null) {
    if (this.dirty) return;
    if (typeof identity !== 'number' || !this.active.delete(identity)) { this.invalidate(); return; }
    this.journal.finished_total++;
    if (decision === 'alert') this.journal.occurrence_count++;
    this.persist();
    await this.queue; // bounded observer work, never business authorization
  }
  async snapshot(nonce: string): Promise<JsonRecord> {
    try {
      await this.bounded((async () => {
        await this.store.identity();
        const current = await this.store.read('enrollment.json');
        check(canonical(current) === canonical(this.enrollment));
        await verifyArtifacts(this.enrollment, this.loaded);
      })());
    } catch { this.invalidate(); }
    const unavailable = () => ({kind: 'hub_alert_evidence_unavailable_v1', available: false, reason: 'coverage_unknown', nonce});
    if (this.dirty || this.pending || this.active.size) return unavailable();
    const version = canonical(this.journal);
    try {
      let stored: unknown;
      await this.bounded(this.store.verify().then(value => { stored = value; }));
      if (this.pending || this.active.size || canonical(this.journal) !== version) return unavailable();
      check(stored === version);
    } catch { this.invalidate(); }
    if (this.dirty) return unavailable();
    return { kind: 'hub_controller_coverage_v1', producer: this.enrollment.producer, recipient: RECIPIENT,
      source_id: this.enrollment.source_id, release_sha: this.enrollment.release_sha, artifact_proof: 'loaded_and_current_v1',
      epoch: hash(this.enrollment.enrollment_id + ':' + this.boot), controller_id: this.boot,
      limitations: [], complete: true, started_total: this.journal.started_total,
      finished_total: this.journal.finished_total, baseline_count: 0, baseline_invocations: 0,
      occurrence_count: this.journal.occurrence_count, observed_at_ms: Date.now(), nonce };
  }
  async close() { this.invalidate(); await this.queue; await this.store.close(); }
}
