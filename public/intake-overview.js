/* Authorized projection; never replace an unavailable count with zero. */
(() => {
  const node = document.getElementById('intake-forgotten');
  if (!node) return;
  async function update() {
    try {
      const response = await fetch('/api/intake/forgotten', {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) throw new Error('unavailable');
      const data = await response.json();
      if (!Number.isInteger(data.forgotten_count)) throw new Error('invalid');
      node.textContent = 'Forgotten: ' + data.forgotten_count;
    } catch { node.textContent = 'Forgotten: unavailable — sign in or retry'; }
  }
  /* Merge gate (2e00e01f): intakes whose work is not merged and deployed. */
  const unmergedNode = document.getElementById('intake-unmerged'), unmergedList = document.getElementById('intake-unmerged-list');
  async function updateUnmerged() {
    if (!unmergedNode) return;
    try {
      const response = await fetch('/api/intake/merge-state', {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) throw new Error('unavailable');
      const data = await response.json();
      if (!Number.isInteger(data.counts?.total_unmerged) || !Array.isArray(data.unmerged)) throw new Error('invalid');
      unmergedNode.textContent = 'Unmerged: ' + data.counts.total_unmerged;
      unmergedList.replaceChildren();
      for (const item of data.unmerged) {
        const row = document.createElement('p');
        const link = document.createElement('a');
        link.href = '/intake/' + encodeURIComponent(item.task_id);
        link.textContent = item.title || item.task_id;
        row.append(link, ` · ${item.state} · requeues ${item.requeues} · ${(item.problems || []).join('; ')}`);
        unmergedList.append(row);
      }
    } catch { unmergedNode.textContent = 'Unmerged: unavailable — sign in or retry'; }
  }
  updateUnmerged();
  setInterval(updateUnmerged, 60000);
  const queue = document.getElementById('intake-queue');
  let queueCursor = '', queueGeneration = 0;
  async function updateQueue(cursor = queueCursor) {
    if (!queue) return;
    const generation = ++queueGeneration;
    queueCursor = cursor;
    try {
      const response = await fetch('/api/intake/queue' + (cursor ? '?cursor=' + encodeURIComponent(cursor) : ''), {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) throw new Error('unavailable');
      const data = await response.json();
      if (!Array.isArray(data.items)) throw new Error('invalid');
      if (generation !== queueGeneration) return;
      queueCursor = cursor;
      queue.replaceChildren();
      const page = document.createElement('p');
      page.textContent = 'Open intakes · queue positions are within this page.';
      queue.append(page);
      if (!data.items.length) {
        const empty = document.createElement('p');
        empty.textContent = data.next_cursor ? 'No visible open intakes on this page.' : 'No visible open intakes on this page. Return to the first page to check earlier work.';
        queue.append(empty);
      }
      for (const item of data.items) {
        const row = document.createElement('details');
        const title = document.createElement('summary');
        const flags = [item.promoted_at ? 'promoted' : '', item.priority && item.priority !== 'normal' ? item.priority : ''].filter(Boolean).join(' · ');
        title.textContent = `${item.stuck ? '⚠ Stuck · ' : ''}${item.queue_position ? '#' + item.queue_position + ' · ' : ''}${flags ? flags + ' · ' : ''}${item.state} · ${item.title}`;
        row.append(title);
        const info = document.createElement('p');
        info.textContent = `${item.reason} · last movement ${new Date(item.last_movement_at).toLocaleString()} · attempts ${item.attempts}/${item.retry_limit}`;
        row.append(info);
        const link = document.createElement('a');
        link.href = '/intake/' + encodeURIComponent(item.id);
        link.textContent = 'Open intake'; row.append(link);
        if (item.state === 'queued') {
          const promote = document.createElement('button');
          promote.textContent = 'Promote to front';
          promote.onclick = async () => {
            promote.disabled = true;
            try {
              const shown = await fetch('/api/intake/tasks/' + encodeURIComponent(item.id), {credentials: 'same-origin', cache: 'no-store'});
              if (!shown.ok) throw new Error('unavailable');
              const version = (await shown.json())?.task?.control?.version;
              const response = await fetch('/api/intake/tasks/' + encodeURIComponent(item.id) + '/promote', {
                method: 'POST', credentials: 'same-origin', cache: 'no-store',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({key: 'portal-promote-' + crypto.randomUUID(), expected_version: version}),
              });
              if (!response.ok) throw new Error('unavailable');
              await updateQueue('');
            } catch { promote.disabled = false; promote.textContent = 'Promote failed — retry'; }
          };
          row.append(promote);
        }
        if (item.completion_evidence) {
          const evidence = document.createElement('p');
          evidence.textContent = 'DONE note includes live evidence; independent verification remains required.';
          row.append(evidence);
        }
        const history = document.createElement('ol');
        for (const event of item.history || []) {
          const entry = document.createElement('li');
          entry.textContent = `${new Date(event.at).toLocaleString()} · ${event.state} · ${event.reason}`;
          history.append(entry);
        }
        row.append(history); queue.append(row);
      }
      if (cursor) {
        const first = document.createElement('button');
        first.textContent = 'First page'; first.onclick = () => updateQueue(''); queue.append(first);
      }
      if (data.next_cursor) {
        const next = document.createElement('button');
        next.textContent = 'Next page'; next.onclick = () => updateQueue(data.next_cursor); queue.append(next);
      }
    } catch { if (generation === queueGeneration) queue.textContent = 'Queue unavailable — sign in or retry'; }
  }
  const throughput = document.getElementById('intake-throughput');
  async function updateThroughput() {
    if (!throughput) return;
    try {
      const response = await fetch('/api/ops/liveness', { credentials: 'same-origin', cache: 'no-store' });
      if (!response.ok) throw new Error('unavailable');
      const data = (await response.json()).throughput;
      if (!data || data.error) throw new Error('unavailable');
      const capacity = data.active_vs_capacity;
      const holds = Object.entries(data.holds_by_reason || {}).map(([reason, n]) => `${reason}: ${n}`).join(' · ') || 'none';
      throughput.textContent = `24h: started ${data.started_24h ?? '?'} (${data.automatic_started_24h ?? '?'} automatic) · closed ${data.closed_24h ?? '?'} · created ${data.created_24h ?? '?'}` +
        ` · automatic active/capacity ${capacity.active_sessions ?? '?'}/${capacity.effective_capacity ?? '?'} · holds: ${holds}` +
        (data.complete === false ? ' · partial metrics: observation limit reached' : '') +
        (data.alert ? ` · ⚠ ${data.alert}` : '');
    } catch { throughput.textContent = '24h throughput: unavailable — sign in or retry'; }
  }
  updateThroughput();
  setInterval(updateThroughput, 60000);
  updateQueue();
  setInterval(updateQueue, 60000);
  update();
  setInterval(update, 60000);
})();
