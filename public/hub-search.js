const GROUPS = [
  ['sessions', 'Sessions'], ['tasks', 'Tasks'], ['machines', 'Machines'],
];

function appendHighlighted(node, value, query) {
  const text = String(value ?? '');
  const at = query ? text.toLocaleLowerCase().indexOf(query.toLocaleLowerCase()) : -1;
  if (at < 0) { node.textContent = text; return; }
  node.append(document.createTextNode(text.slice(0, at)));
  const mark = document.createElement('mark'); mark.textContent = text.slice(at, at + query.length); node.append(mark);
  node.append(document.createTextNode(text.slice(at + query.length)));
}

export function mountHubSearch(trigger) {
  if (document.querySelector('[data-hub-search]')) return;
  const dialog = document.createElement('section');
  dialog.className = 'hub-search'; dialog.dataset.hubSearch = 'true'; dialog.hidden = true;
  dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true'); dialog.setAttribute('aria-labelledby', 'hub-search-title');
  const panel = document.createElement('div'); panel.className = 'hub-search__panel';
  const title = document.createElement('h2'); title.id = 'hub-search-title'; title.textContent = 'Search the Hub';
  const form = document.createElement('form'); form.className = 'hub-search__form'; form.setAttribute('role', 'search');
  const input = document.createElement('input'); input.type = 'search'; input.placeholder = 'Search sessions, tasks, machines…'; input.setAttribute('aria-label', 'Search sessions, tasks, and machines'); input.autocomplete = 'off'; input.spellcheck = false;
  const scope = document.createElement('select'); scope.setAttribute('aria-label', 'Search scope');
  scope.append(new Option('Active', 'active'), new Option('All, including finished', 'all'));
  const typeSelect = document.createElement('select'); typeSelect.setAttribute('aria-label', 'Result type');
  typeSelect.append(new Option('All types', 'all'), ...GROUPS.map(([value, label]) => new Option(label, value)));
  const project = document.createElement('input'); project.type = 'text'; project.placeholder = 'Project'; project.setAttribute('aria-label', 'Filter by project'); project.maxLength = 128;
  const host = document.createElement('input'); host.type = 'text'; host.placeholder = 'Host'; host.setAttribute('aria-label', 'Filter by host'); host.maxLength = 128;
  const status = document.createElement('input'); status.type = 'text'; status.placeholder = 'Status'; status.setAttribute('aria-label', 'Filter by status'); status.maxLength = 64;
  const close = document.createElement('button'); close.type = 'button'; close.className = 'hub-search__close'; close.textContent = 'Close';
  form.append(input, scope, typeSelect, project, host, status, close);
  const statusLine = document.createElement('p'); statusLine.className = 'hub-search__state'; statusLine.setAttribute('role', 'status'); statusLine.setAttribute('aria-live', 'polite');
  const groups = document.createElement('div'); groups.className = 'hub-search__results';
  const more = document.createElement('button'); more.type = 'button'; more.className = 'hub-search__more'; more.textContent = 'Search more'; more.hidden = true;
  panel.append(title, form, statusLine, groups, more); dialog.append(panel); document.body.append(dialog);

  let controller = null, requestNo = 0, timer = 0, cursor = null, selected = -1, busy = false, retryCurrent = false;
  let lastFocus = null, accumulated = [];
  const paramsFromLocation = () => new URLSearchParams(location.search);
  const syncLocation = (open) => {
    const url = new URL(location.href);
    if (input.value) url.searchParams.set('q', input.value); else url.searchParams.delete('q');
    if (scope.value === 'all') url.searchParams.set('scope', 'all'); else url.searchParams.delete('scope');
    if (typeSelect.value !== 'all') url.searchParams.set('type', typeSelect.value); else url.searchParams.delete('type');
    for (const [name, field] of [['project', project], ['host', host], ['status', status]]) {
      if (field.value.trim()) url.searchParams.set(name, field.value.trim()); else url.searchParams.delete(name);
    }
    if (open) url.searchParams.set('hubSearch', '1'); else url.searchParams.delete('hubSearch');
    history.replaceState(history.state, '', url);
  };
  const closeDialog = () => {
    if (dialog.hidden) return;
    dialog.hidden = true; controller?.abort(); syncLocation(false);
    lastFocus?.focus?.();
  };
  const openDialog = () => {
    lastFocus = document.activeElement;
    const params = paramsFromLocation();
    input.value = params.get('q') || '';
    scope.value = params.get('scope') === 'all' ? 'all' : 'active';
    typeSelect.value = ['sessions', 'tasks', 'machines'].includes(params.get('type')) ? params.get('type') : 'all';
    project.value = params.get('project') || '';
    host.value = params.get('host') || '';
    status.value = params.get('status') || '';
    dialog.hidden = false; syncLocation(true); input.focus(); runSearch(false);
  };
  const allResults = () => [...groups.querySelectorAll('[role="option"]')];
  const moveSelection = delta => {
    const options = allResults();
    if (!options.length) return;
    selected = (selected + delta + options.length) % options.length;
    options.forEach((el, index) => { el.setAttribute('aria-selected', String(index === selected)); if (index === selected) el.scrollIntoView({ block: 'nearest' }); });
  };
  const selectResult = () => allResults()[selected]?.click();
  const render = (append = false) => {
    if (!append) groups.replaceChildren();
    const grouped = new Map(GROUPS.map(([kind, label]) => [kind, { label, items: [] }]));
    for (const item of accumulated) grouped.get(item.kind)?.items.push(item);
    for (const [kind, group] of grouped) {
      if (!group.items.length) continue;
      let section = append ? groups.querySelector(`[data-kind="${kind}"]`) : null;
      if (!section) {
        section = document.createElement('section'); section.className = 'hub-search__group'; section.dataset.kind = kind;
        const h = document.createElement('h3'); h.textContent = group.label; section.append(h);
        const list = document.createElement('div'); list.className = 'hub-search__list'; section.append(list); groups.append(section);
      }
      const list = section.querySelector('.hub-search__list');
      const already = append ? list.childElementCount : 0;
      for (const item of group.items.slice(already)) {
        const a = document.createElement('a'); a.className = 'hub-search__result'; a.href = item.href; a.setAttribute('role', 'option'); a.setAttribute('aria-selected', 'false');
        const resultUrl = new URL(item.href, location.origin);
        for (const key of ['q', 'scope', 'type', 'project', 'host', 'status']) {
          const value = key === 'q' ? input.value.trim() : key === 'scope' ? scope.value : key === 'type' ? typeSelect.value : ({ project, host, status })[key].value.trim();
          if (value && value !== 'all' && (key !== 'scope' || value !== 'active')) resultUrl.searchParams.set(key, value);
        }
        resultUrl.searchParams.delete('hubSearch');
        a.href = resultUrl.pathname + resultUrl.search + resultUrl.hash;
        const name = document.createElement('strong'); appendHighlighted(name, item.title, input.value);
        const meta = document.createElement('span'); meta.className = 'hub-search__meta';
        const state = document.createElement('span'); state.textContent = item.status;
        const context = document.createElement('span'); appendHighlighted(context, item.context, input.value);
        const id = document.createElement('span'); id.className = 'hub-search__id'; appendHighlighted(id, item.id, input.value);
        meta.append(state); if (item.context) meta.append(context);
        a.append(name, meta, id);
        a.addEventListener('mouseenter', () => { selected = allResults().indexOf(a); allResults().forEach((el, i) => el.setAttribute('aria-selected', String(i === selected))); });
        list.append(a);
      }
    }
    selected = -1;
  };

  async function runSearch(loadMore = false) {
    const query = input.value.trim(); retryCurrent = false;
    if (query.length < 2) {
      controller?.abort(); cursor = null; accumulated = []; groups.replaceChildren(); more.hidden = true;
      statusLine.textContent = 'Enter at least two characters. Active scope excludes finished work.';
      syncLocation(true); return;
    }
    if (!loadMore) { cursor = null; accumulated = []; }
    controller?.abort(); controller = new AbortController();
    const requestId = ++requestNo; busy = true; more.disabled = true;
    statusLine.textContent = loadMore ? 'Searching more records…' : 'Searching…'; syncLocation(true);
    try {
      let data, pages = 0;
      do {
        const params = new URLSearchParams({ q: query, scope: scope.value, limit: '12' });
        if (typeSelect.value !== 'all') params.append('type', typeSelect.value);
        if (project.value.trim()) params.set('project', project.value.trim());
        if (host.value.trim()) params.set('host', host.value.trim());
        if (status.value.trim()) params.set('status', status.value.trim());
        if (cursor) params.set('cursor', cursor);
        const response = await fetch(`/api/ui-search?${params}`, { credentials: 'same-origin', cache: 'no-store', signal: controller.signal, headers: { Accept: 'application/json' } });
        if (requestId !== requestNo) return;
        data = await response.json();
        if (!response.ok) {
          const message = data.error === 'search_indexing' ? 'Search index is still starting. Try again shortly.'
            : data.error === 'invalid_status' ? 'Use a supported status filter.'
            : response.status === 401 ? 'Sign in to search.'
            : response.status === 403 ? 'Search is available to Hub operators.'
            : 'Search is temporarily unavailable.';
          throw new Error(message);
        }
        cursor = data.next_cursor || null;
        accumulated = [...accumulated, ...(data.results || [])];
        pages++;
      } while (!loadMore && cursor && accumulated.length < 12 && pages < 5 && data?.partial);
      if (requestId !== requestNo) return;
      render(loadMore);
      const count = accumulated.length;
      statusLine.textContent = data.partial && cursor
        ? `${count} result${count === 1 ? '' : 's'} · ${data.scope === 'all' ? 'All records' : 'Active records'} · more records remain`
        : `${count} result${count === 1 ? '' : 's'} · ${data.scope === 'all' ? 'All records, including finished' : 'Active records'} · complete`;
      if (!count && !data.partial) statusLine.textContent = `No matches in ${data.scope === 'all' ? 'all records' : 'active records'}.`;
      more.hidden = !cursor; more.disabled = false; more.textContent = data.partial && !count ? 'Continue searching' : 'Search more';
    } catch (error) {
      if (error.name === 'AbortError' || requestId !== requestNo) return;
      statusLine.textContent = error.message || 'Search failed. Try again.';
      retryCurrent = true; more.textContent = 'Retry search'; more.hidden = false;
    } finally {
      if (requestId === requestNo) { busy = false; more.disabled = false; }
    }
  }

  const scheduleSearch = () => {
    clearTimeout(timer);
    controller?.abort();
    requestNo++;
    busy = false;
    more.disabled = false;
    timer = setTimeout(() => runSearch(false), 180);
  };
  trigger.addEventListener('click', openDialog);
  document.addEventListener('hub:search-open', openDialog);
  close.addEventListener('click', closeDialog);
  more.addEventListener('click', () => { if (busy) return; if (retryCurrent) runSearch(false); else if (cursor) runSearch(true); });
  dialog.addEventListener('click', event => { if (event.target === dialog) closeDialog(); });
  form.addEventListener('submit', event => { event.preventDefault(); clearTimeout(timer); runSearch(false); });
  for (const field of [input, scope, typeSelect, project, host, status]) field.addEventListener('input', scheduleSearch);
  scope.addEventListener('change', scheduleSearch); typeSelect.addEventListener('change', scheduleSearch);
  dialog.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); closeDialog(); }
    else if (event.key === 'ArrowDown') { event.preventDefault(); moveSelection(1); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); moveSelection(-1); }
    else if (event.key === 'Enter' && document.activeElement !== more) { event.preventDefault(); if (selected >= 0) selectResult(); else runSearch(false); }
    else if (event.key === 'Tab') {
      const focusable = [...dialog.querySelectorAll('input:not(:disabled),select:not(:disabled),button:not(:disabled),a[href]')].filter(el => !el.hidden);
      if (!focusable.length) return;
      if (event.shiftKey && document.activeElement === focusable[0]) { event.preventDefault(); focusable.at(-1).focus(); }
      else if (!event.shiftKey && document.activeElement === focusable.at(-1)) { event.preventDefault(); focusable[0].focus(); }
    }
  });
  document.addEventListener('keydown', event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); if (dialog.hidden) openDialog(); else input.focus(); }
    else if (!dialog.hidden && event.key === 'Escape') closeDialog();
  });
  window.addEventListener('popstate', () => {
    const params = paramsFromLocation();
    if (params.get('hubSearch') === '1' && dialog.hidden) openDialog();
    else if (params.get('hubSearch') !== '1' && !dialog.hidden) closeDialog();
  });
  if (new URLSearchParams(location.search).get('hubSearch') === '1') openDialog();
}
