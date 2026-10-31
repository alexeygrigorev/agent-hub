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
        title.textContent = `${item.stuck ? '⚠ Stuck · ' : ''}${item.queue_position ? '#' + item.queue_position + ' · ' : ''}${item.state} · ${item.title}`;
        row.append(title);
        const info = document.createElement('p');
        info.textContent = `${item.reason} · last movement ${new Date(item.last_movement_at).toLocaleString()} · attempts ${item.attempts}/${item.retry_limit}`;
        row.append(info);
        const link = document.createElement('a');
        link.href = '/intake/' + encodeURIComponent(item.id);
        link.textContent = 'Open intake'; row.append(link);
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
  updateQueue();
  setInterval(updateQueue, 60000);
  update();
  setInterval(update, 60000);
})();
