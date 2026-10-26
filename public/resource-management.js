/* Authenticated overview measurements. Text only: host metadata is never HTML. */
(() => {
  async function refresh() {
    const root = document.getElementById('resource-management-summary');
    if (!root) return;
    const number = value => Number.isFinite(value) ? value.toFixed(1) : 'unknown';
    const gib = value => number(Number.isFinite(value) ? value / 1024 : null);
    try {
      const response = await fetch('/api/fleet/resources', { credentials:'same-origin', cache:'no-store' });
      if (!response.ok) throw new Error('unavailable');
      const data = await response.json();
      root.replaceChildren();
      for (const host of data.hosts || []) {
        const p = document.createElement('p');
        p.textContent = `${host.machine_id}: memory ${gib(host.mem_used_mb)} / ${gib(host.mem_total_mb)} GiB; load5 ${number(host.load5)} / ${number(host.cores)} cores; ${host.running} running, ${host.queued} queued. Floor ${gib(host.floor_mb)} GiB; starting reserve ${number(host.reserved_mb)} MiB.`;
        root.append(p);
      }
      const table = document.createElement('table');
      const header = document.createElement('tr');
      for (const label of ['Session / engine', 'Peak RSS / PSS MiB', 'CPU seconds / average cores', 'Runtime / state']) {
        const th = document.createElement('th'); th.textContent = label; header.append(th);
      }
      table.append(header);
      for (const session of (data.sessions || []).slice(0, 12)) {
        const tr = document.createElement('tr');
        const values = [`${session.session_id} / ${session.engine || 'unknown'}`, `${number(session.peak_rss_mb)} / ${number(session.peak_pss_mb)}${session.rss_flagged ? ' · high RSS' : ''}`, `${number(session.cpu_seconds)} / ${number(session.avg_cpu_cores)}`, `${number(session.runtime_s)}s / ${session.state}`];
        for (const value of values) { const td=document.createElement('td'); td.textContent=value; tr.append(td); }
        table.append(tr);
      }
      if ((data.sessions || []).length) root.append(table);
      for (const stat of (data.stats || []).slice(0, 6)) {
        const p=document.createElement('p');
        p.textContent=`${stat.project} / ${stat.engine} / ${stat.kind}: ${stat.samples} samples; RSS p50 ${number(stat.p50_rss_mb)}, p90 ${number(stat.p90_rss_mb)} MiB.`;
        root.append(p);
      }
      for (const reap of (data.reaps || []).slice(0, 3)) {
        const p=document.createElement('p');
        p.textContent=`${reap.at}: ${reap.actor} reaped ${reap.session_id} (${reap.reason}); freed ${number(reap.freed_mb)} MiB. Intake ${reap.intake_id || 'unknown'}.`;
        root.append(p);
      }
      if (!(data.hosts || []).length) root.textContent='No host resource measurements reported yet.';
    } catch {
      root.textContent='Resource measurements unavailable. Sign in with operator access or retry after the next host report.';
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', refresh, {once:true});
  else refresh();
  setInterval(refresh, 60000);
})();
