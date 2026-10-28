/* Hub Inbox page (docs/SESSION-MESSAGING.md §9, AC4): the human's own inbox
 * and delegated named-agent inboxes. Cookie session + CSRF like the other
 * pages; no tokens or message bodies in storage. Phone width stacks the
 * split view (inbox.css). */
(function () {
  'use strict';

  var state = { me: null, csrf: null, inboxes: [], actor: null, messages: [], selected: null, unreadOnly: false, timer: null };

  function byId(id) { return document.getElementById(id); }
  function esc(text) {
    var div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
  }
  function when(ts) {
    if (!ts) return '';
    var ms = typeof ts === 'number' ? ts : Date.parse(ts);
    if (!Number.isFinite(ms)) return '';
    var diff = Date.now() - ms;
    if (diff < 60000) return 'just now';
    if (diff < 3600000) return Math.floor(diff / 60000) + ' min ago';
    if (diff < 86400000) return Math.floor(diff / 3600000) + ' h ago';
    return new Date(ms).toLocaleString();
  }

  async function api(path, options) {
    options = options || {};
    var headers = {};
    if (options.body !== undefined) { headers['Content-Type'] = 'application/json'; headers['X-CSRF-Token'] = state.csrf; }
    var response = await fetch(path, { method: options.method || 'GET', headers: headers, credentials: 'same-origin', cache: 'no-store', body: options.body === undefined ? undefined : JSON.stringify(options.body) });
    if (response.status === 401) { globalThis.location.assign('/auth/login'); throw new Error('Sign-in required'); }
    if (!response.ok) {
      var detail = '';
      try { var payload = await response.json(); if (payload && payload.error) detail = String(payload.error); } catch (e) { /* not JSON */ }
      throw new Error('Hub request failed (' + response.status + (detail ? '; ' + detail : '') + ')');
    }
    return response.json();
  }

  function setStatus(text) { byId('inbox-state').textContent = text; }

  function renderActorSelect() {
    var select = byId('inbox-actor');
    select.replaceChildren();
    for (var inbox of state.inboxes) {
      var option = document.createElement('option');
      option.value = inbox;
      option.textContent = inbox;
      select.appendChild(option);
    }
    select.value = state.actor || '';
    select.disabled = state.inboxes.length < 2;
  }

  function unreadCount() {
    return state.messages.filter(function (m) { return !m.read_at; }).length;
  }

  function renderBadge() {
    var badge = byId('inbox-unread-badge');
    var count = unreadCount();
    badge.hidden = count === 0;
    badge.textContent = count > 99 ? '99+' : String(count);
    badge.removeAttribute('aria-hidden');
    document.title = (count ? '(' + count + ') ' : '') + 'Inbox · Agent Hub';
  }

  function renderList() {
    var list = byId('inbox-list');
    var markRead = byId('inbox-mark-read');
    markRead.hidden = unreadCount() === 0;
    if (!state.messages.length) {
      list.replaceChildren();
      var empty = document.createElement('p');
      empty.className = 'inbox-empty';
      empty.textContent = state.unreadOnly ? 'No unread messages.' : 'No messages.';
      list.appendChild(empty);
      return;
    }
    var seen = {};
    var frag = document.createDocumentFragment();
    for (var msg of state.messages) {
      if (seen[msg.id]) continue;
      seen[msg.id] = true;
      var row = document.createElement('button');
      row.type = 'button';
      row.className = 'inbox-row' + (msg.id === state.selected ? ' inbox-row--active' : '') + (!msg.read_at ? ' inbox-row--unread' : '');
      row.setAttribute('role', 'option');
      row.setAttribute('aria-selected', msg.id === state.selected ? 'true' : 'false');
      row.innerHTML =
        '<span class="inbox-row__top"><span class="inbox-row__from">' + esc(msg.from_actor) + '</span>' +
        '<span class="inbox-row__when">' + esc(when(msg.created_at)) + '</span></span>' +
        '<span class="inbox-row__preview">' + esc(String(msg.body || '').slice(0, 120)) + '</span>' +
        '<span class="inbox-row__meta">' + esc(msg.state || 'accepted') + (msg.thread_id ? ' · thread ' + esc(String(msg.thread_id).slice(0, 8)) : '') + '</span>' +
        (!msg.read_at ? '<span class="inbox-dot" aria-label="unread"></span>' : '');
      row.addEventListener('click', function (m) { return function () { openMessage(m.id); }; }(msg));
      frag.appendChild(row);
    }
    list.replaceChildren(frag);
  }

  async function openMessage(id) {
    state.selected = id;
    renderList();
    var section = byId('inbox-detail-section');
    section.hidden = false;
    if (window.matchMedia('(max-width: 720px)').matches) section.scrollIntoView({ behavior: 'smooth' });
    var detail = byId('inbox-detail');
    detail.innerHTML = '<p class="inbox-empty">Loading…</p>';
    var msg;
    try { msg = await api('/api/bus/messages/' + encodeURIComponent(id)); } catch (e) { detail.innerHTML = '<p class="inbox-empty">' + esc(e.message) + '</p>'; return; }
    var ref = null;
    try { ref = msg.ref_json ? JSON.parse(msg.ref_json) : null; } catch (e) { /* ignore */ }
    detail.innerHTML =
      '<div class="inbox-msg"><p class="inbox-msg__head"><strong>' + esc(msg.from_actor) + '</strong> → <strong>' + esc(msg.to_actor) + '</strong></p>' +
      '<p class="inbox-msg__meta">state ' + esc(msg.state || 'accepted') + ' · ' + esc(when(msg.created_at)) +
      (msg.deliver_by ? ' · deliver by ' + esc(when(msg.deliver_by)) : '') + '</p>' +
      (ref ? '<p class="inbox-msg__ref">ref: <code>' + esc(JSON.stringify(ref)) + '</code></p>' : '') +
      '<pre class="inbox-msg__body">' + esc(msg.body) + '</pre></div>';
    var form = byId('inbox-reply-form');
    form.hidden = false;
    byId('inbox-reply-label').textContent = 'Reply as ' + (state.actor || 'me');
    byId('inbox-reply-meta').textContent = 'thread ' + String(msg.thread_id || '').slice(0, 8) + ' · to ' + msg.from_actor;
    form.dataset.replyTo = msg.id;
    form.dataset.thread = msg.thread_id || '';
    // Reading a message marks it read for the human's own inbox.
    markMessagesRead([msg.id], true);
  }

  async function markMessagesRead(ids, quiet) {
    if (!ids.length) return;
    try {
      await api('/api/bus/messages/read', { method: 'POST', body: { ids: ids } });
      for (var msg of state.messages) if (ids.indexOf(msg.id) >= 0) msg.read_at = msg.read_at || Date.now();
      renderList();
      renderBadge();
    } catch (e) {
      if (!quiet) setStatus(e.message);
    }
  }

  async function sendReply(event) {
    event.preventDefault();
    var form = byId('inbox-reply-form');
    var body = byId('inbox-reply-body').value;
    var button = byId('inbox-reply-send');
    if (!form.dataset.replyTo || !body.trim()) return;
    button.disabled = true;
    setStatus('Sending reply…');
    try {
      var original = state.messages.find(function (m) { return m.id === form.dataset.replyTo; });
      var payload = { reply_to: form.dataset.replyTo, kind: 'reply', body: body, idempotency_key: crypto.randomUUID(), from_actor: state.actor, thread_id: form.dataset.thread || undefined };
      if (original) payload.to_actor = original.from_actor;
      if (payload.to_actor === undefined) {
        // Derive from the original when the list no longer holds it.
        var full = await api('/api/bus/messages/' + encodeURIComponent(form.dataset.replyTo));
        payload.to_actor = full.from_actor;
      }
      if (state.actor && state.actor.indexOf('agent:') === 0) payload.as = state.actor;
      await api('/api/bus/reply', { method: 'POST', body: payload });
      byId('inbox-reply-body').value = '';
      setStatus('Reply sent.');
      await loadMessages();
    } catch (e) {
      setStatus(e.message);
    } finally {
      button.disabled = false;
    }
  }

  async function loadInboxes() {
    var info = await api('/api/bus/inboxes');
    state.inboxes = [];
    if (info.actor) state.inboxes.push(info.actor);
    for (var name of info.named_agents || []) if (state.inboxes.indexOf(name) < 0) state.inboxes.push(name);
    if (!state.actor && state.inboxes.length) state.actor = state.inboxes[0];
    renderActorSelect();
  }

  async function loadMessages() {
    if (!state.actor) return;
    var params = new URLSearchParams({ actor: state.actor, limit: '100' });
    if (state.unreadOnly) params.set('unread', '1');
    var rows = await api('/api/bus/inbox?' + params.toString());
    state.messages = Array.isArray(rows) ? rows : rows.messages || [];
    renderList();
    renderBadge();
    setStatus(state.actor + ' · ' + state.messages.length + ' message' + (state.messages.length === 1 ? '' : 's') + (unreadCount() ? ', ' + unreadCount() + ' unread' : ''));
  }

  async function refresh() {
    try { await loadMessages(); } catch (e) { setStatus(e.message); }
  }

  async function boot() {
    var me;
    try { me = await api('/auth/me'); } catch (e) { return; }
    if (!me || !me.email) { globalThis.location.assign('/auth/login'); return; }
    state.me = me;
    state.csrf = me.csrf_token || null;
    byId('account-name').textContent = me.email;
    byId('account-initials').textContent = me.email.slice(0, 1).toUpperCase();
    try {
      await loadInboxes();
      await loadMessages();
    } catch (e) { setStatus(e.message); }
    byId('inbox-refresh').addEventListener('click', refresh);
    byId('inbox-actor').addEventListener('change', async function (event) {
      state.actor = event.target.value;
      state.selected = null;
      byId('inbox-detail-section').hidden = true;
      await refresh();
    });
    byId('inbox-unread-only').addEventListener('change', async function (event) {
      state.unreadOnly = event.target.checked;
      await refresh();
    });
    byId('inbox-mark-read').addEventListener('click', function () {
      markMessagesRead(state.messages.filter(function (m) { return !m.read_at; }).map(function (m) { return m.id; }));
    });
    byId('inbox-close').addEventListener('click', function () {
      byId('inbox-detail-section').hidden = true;
      state.selected = null;
      renderList();
    });
    byId('inbox-reply-form').addEventListener('submit', sendReply);
    state.timer = setInterval(refresh, 15000);
  }

  addEventListener('DOMContentLoaded', boot);
})();
