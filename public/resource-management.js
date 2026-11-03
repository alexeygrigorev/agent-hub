/* Authenticated overview measurements. Text only: host metadata is never HTML. */
(() => {
  let costHours='24';
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
        p.textContent = `${host.machine_id}: memory ${gib(host.mem_used_mb)} / ${gib(host.mem_total_mb)} GiB; CPU ${number(host.cpu?.current_cpu_cores)} / ${number(host.cpu?.limit_cpu_cores)} cores advisory target; ${host.cpu?.reason || (host.cpu?.telemetry_state === 'unknown' ? 'CPU telemetry unknown (advisory)' : 'no measured choking')}; load5 ${number(host.load5)} / ${number(host.cores)} cores; ${host.running} running, ${host.queued} queued. Floor ${gib(host.floor_mb)} GiB; starting reserve ${number(host.reserved_mb)} MiB.`;
        root.append(p);
      }
      const title=document.createElement('h3'); title.textContent='Per-task resource costs'; root.append(title);
      const picker=document.createElement('select'); picker.setAttribute('aria-label','Task cost period');
      for (const [value,label] of [['24','Last 24 hours'],['168','Last 7 days']]) {
        const option=document.createElement('option'); option.value=value; option.textContent=label; picker.append(option);
      }
      picker.value=costHours; root.append(picker);
      const costs=document.createElement('div'); root.append(costs);
      function renderCosts() {
        costs.replaceChildren();
        const cutoff=Date.now()-Number(costHours)*3600000;
        const sessions=(data.sessions || []).filter(s=>s.finished_at && Date.parse(s.finished_at)>=cutoff);
        const coverage=document.createElement('p');
        coverage.textContent=`${sessions.length} finished sessions in the most recent ${data.sessions?.length || 0} ledger rows (maximum 500). Unknown token and resource measurements are excluded from measured totals.`;
        costs.append(coverage);
        function table(headers,rows) {
          const wrap=document.createElement('div'); wrap.style.overflowX='auto';
          const table=document.createElement('table'); const head=document.createElement('tr');
          for(const label of headers) { const th=document.createElement('th'); th.textContent=label; th.scope='col'; head.append(th); }
          const thead=document.createElement('thead'); thead.append(head); table.append(thead);
          const body=document.createElement('tbody');
          for(const values of rows) { const tr=document.createElement('tr'); for(const value of values) { const td=document.createElement('td'); td.textContent=value; tr.append(td); } body.append(tr); }
          table.append(body); wrap.append(table); costs.append(wrap);
        }
        table(['Task / project','Engine / model / tier','Tokens in / out / total','Peak RSS / PSS MiB','CPU seconds / average cores','Runtime / final state'],sessions.map(s=>[
          `${s.session_id} / ${s.project || 'unknown'} / ${s.kind || 'unknown'}`,
          `${s.engine || 'unknown'} / ${s.model || 'unknown'} / ${s.tier || 'unknown'}`,
          s.tokens_known ? `${s.input_tokens} / ${s.output_tokens} / ${s.total_tokens}` : `unknown (${s.token_join_reason || 'unproven'})`,
          `${number(s.peak_rss_mb)} / ${number(s.peak_pss_mb)}`,`${number(s.cpu_seconds)} / ${number(s.avg_cpu_cores)}`,
          `${number(s.runtime_s)}s / ${s.state}`]));
        for(const dimension of ['project','engine']) {
          const totals=data.cost_totals?.[costHours==='24' ? '24h' : '7d']?.[dimension] || [];
          table([`Full-period totals per ${dimension}`,'Sessions / tokens known','Measured tokens','Measured CPU seconds','Measured runtime seconds','Maximum RSS MiB'],totals.map(s=>[
            s.name || 'unknown',`${s.sessions} / ${s.tokens_known_sessions}`,number(s.total_tokens),number(s.cpu_seconds),number(s.runtime_s),number(s.peak_rss_mb)]));
        }
      }
      picker.addEventListener('change',()=>{costHours=picker.value;renderCosts();}); renderCosts();
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
  // Intake 94186660: hidden tabs skip the 60s poll.
  setInterval(() => { if (!document.hidden) refresh(); }, 60000);
})();
