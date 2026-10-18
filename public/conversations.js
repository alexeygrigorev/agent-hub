/* Conversations UI for the agent hub: list, thread, composer, retry with
   preserved drafts and idempotency keys, reconnect and read position.
   All server data is rendered as text (textContent / createElement); message
   content is never parsed as HTML.
   Transport contract: the M1 conversations API
   (docs/SPEC.md milestone M1; contract doc M1-UI-REPAIR.md). Real shapes are
   normalized once in the "contract adapter" section below; the rendering layer
   only ever sees the normalized view model. Identities are opaque canonical
   actor strings (human:<sub>, <project>:<tag>); the verified self identity
   comes from /api/conversations/participants (self.actor), never from an
   inferred email or admin flag. */
(function () {
  'use strict';

  /* Theme boot, before first paint: stored choice wins, else OS preference. */
  try {
    var storedTheme = globalThis.localStorage.getItem('dakit-theme');
    document.documentElement.dataset.theme =
      storedTheme === 'dark' || storedTheme === 'light'
        ? storedTheme
        : (globalThis.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  } catch (e) { /* blocked storage keeps the default theme */ }

  var CONV_API = {
    me: '/auth/me',
    login: '/auth/login',
    logout: '/auth/logout',
    conversations: '/api/conversations',
    participants: '/api/conversations/participants',
    conversation: function (id) { return '/api/conversations/' + encodeURIComponent(id); },
    messages: function (id) { return '/api/conversations/' + encodeURIComponent(id) + '/messages'; },
    read: function (id) { return '/api/conversations/' + encodeURIComponent(id) + '/read'; },
    followThrough: function (id) { return '/api/conversations/' + encodeURIComponent(id) + '/follow-through'; },
    links: function (id) { return '/api/conversations/' + encodeURIComponent(id) + '/links'; },
    link: function (id, linkId) { return '/api/conversations/' + encodeURIComponent(id) + '/links/' + encodeURIComponent(linkId); },
    historyLimit: 50
  };
  var LIST_POLL_MS = 30000;
  var THREAD_POLL_MS = 10000;
  var POLL_MAX_MS = 60000;
  var PHONE = '(max-width: 820px)';

  var state = {
    me: null,           // /auth/me chrome info; display only, never an identity source
    selfActor: null,    // verified conversation identity: participants.self.actor
    directory: {},      // actor -> display label (from participants)
    participants: [],   // normalized, self excluded from pickers
    conversations: [],  // normalized list summaries
    details: {},        // id -> normalized detail (active members)
    detailErrors: {},   // id -> error message when the detail fetch failed
    currentId: null,
    threads: {},        // id -> thread state (messages keyed/ordered by seq)
    outbox: {},         // id -> [{ clientId, body, idempotencyKey, pending, error }]
    participantsLoaded: false,
    listTimer: null,
    threadTimer: null,
    listBackoff: 0,
    threadBackoff: 0,
    markedReadFor: null,
    sending: false,
    offline: !globalThis.navigator.onLine,
    createKey: null,    // idempotency key for the dialog's canonical payload
    createFingerprint: null,
    signedOut: false,
    authGeneration: 0,
    routeGeneration: 0,
    followThrough: {}
  };

  function byId(id) { return document.getElementById(id); }
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function svgIcon(path, label) {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.8');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    var p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('d', path);
    svg.append(p);
    if (label) svg.setAttribute('aria-label', label);
    return svg;
  }
  function announce(text) {
    if (globalThis.dakitShell) globalThis.dakitShell.announce(text);
  }

  /* ---- transport ----
     Session cookie auth plus X-CSRF-Token (from /auth/me csrf_token) on every
     mutating request. 401 redirects to login once; 403 is never retried in a
     loop — membership and permission failures surface as stopped errors and
     drafts are preserved. Only csrf_invalid refreshes the token and retries,
     exactly once. */

  var csrf = null;
  var requests = new Set();
  var sessionRequest = null;
  var sessionController = null;
  function context(routeBound) {
    return { auth: state.authGeneration, route: routeBound ? state.routeGeneration : null };
  }
  function current(ctx) {
    return ctx.auth === state.authGeneration && (ctx.route === null || ctx.route === state.routeGeneration);
  }
  function stale() { return { kind: 'stale', message: 'Request superseded.' }; }
  function checkCurrent(ctx) { if (!current(ctx)) throw stale(); }
  function abortRequests(routeOnly) {
    for (var request of requests) {
      if (!routeOnly || request.routeBound) request.controller.abort();
    }
  }
  function changeRoute(id) {
    if (state.currentId === id) return;
    state.routeGeneration++;
    abortRequests(true);
    clearTimeout(state.threadTimer);
    state.currentId = id;
    state.sending = false;
    /* A textarea belongs to both its actor and conversation. */
    byId('conv-detail').replaceChildren();
  }
  function resetActor(actor, keepScopedDisk) {
    state.authGeneration++;
    if (sessionController) sessionController.abort();
    abortRequests(false);
    clearTimeout(state.listTimer);
    clearTimeout(state.threadTimer);
    state.selfActor = actor;
    state.outbox = {};
    state.threads = {};
    state.followThrough = {};
    state.details = {};
    state.detailErrors = {};
    state.conversations = [];
    state.directory = {};
    state.participants = [];
    state.participantsLoaded = false;
    state.markedReadFor = null;
    state.sending = false;
    state.createKey = null;
    state.createFingerprint = null;
    var dialog = byId('new-conversation-dialog');
    if (dialog.open) dialog.close();
    var newBtn = byId('new-conversation');
    if (newBtn) newBtn.disabled = !actor || !state.participantsLoaded;
    byId('new-conversation-form').reset();
    byId('identity-list').replaceChildren();
    /* Clear old pending rows and unsent text before rendering new chrome. */
    byId('conv-detail').replaceChildren();
    purgeOutboxes(actor, keepScopedDisk);
    renderConversations();
    render();
  }
  function forgetSession() {
    state.signedOut = true;
    if (sessionController) sessionController.abort();
    resetActor(null);
    state.me = null;
    csrf = null;
  }
  async function api(path, options) {
    options = options || {};
    var ctx = options.context || context(options.routeBound);
    checkCurrent(ctx);
    var request = { controller: new AbortController(), routeBound: !!options.routeBound };
    requests.add(request);
    try {
      var headers = {};
      if (options.body !== undefined) {
        headers['Content-Type'] = 'application/json';
        headers['X-CSRF-Token'] = csrf;
      }
      var response;
      try {
        response = await fetch(path, {
          method: options.method || 'GET',
          headers: headers,
          credentials: 'same-origin',
          cache: 'no-store',
          signal: request.controller.signal,
          body: options.body === undefined ? undefined : JSON.stringify(options.body)
        });
      } catch (networkError) {
        if (request.controller.signal.aborted || !current(ctx)) throw stale();
        throw { kind: 'offline', message: 'You appear to be offline. Reconnect and retry.' };
      }
      checkCurrent(ctx);
      if (response.status === 401) {
        forgetSession();
        globalThis.location.assign(CONV_API.login);
        throw { kind: 'auth', message: 'Sign-in required.' };
      }
      if (response.status === 403) {
        var detail403 = '';
        try { var payload403 = await response.json(); if (payload403 && payload403.error) detail403 = String(payload403.error); } catch (e) { /* not JSON */ }
        checkCurrent(ctx);
        if (detail403 === 'csrf_invalid' && !options.csrfRetried) {
          var refreshed = await refreshSession();
          checkCurrent(ctx); // never replay an old actor's mutation with a new session
          if (refreshed === 'ok') return api(path, Object.assign({}, options, { csrfRetried: true, context: ctx }));
        }
        throw {
          kind: 'forbidden',
          status: 403,
          message: detail403 === 'csrf_invalid'
            ? 'Your session could not be verified. Reload the page and try again.'
            : 'You do not have access to this conversation.'
        };
      }
      if (!response.ok) {
        var detail = '';
        try { var payload = await response.json(); if (payload && payload.error) detail = String(payload.error); } catch (e) { /* body not JSON */ }
        throw { kind: 'server', status: response.status, message: 'Hub request failed (' + response.status + (detail ? '; ' + detail : '') + ').' };
      }
      if (response.status === 204) return null;
      var data = await response.json();
      checkCurrent(ctx);
      if (data && data.self_actor && data.self_actor !== state.selfActor) {
        resetActor(null);
        refreshSession();
        throw stale();
      }
      return data;
    } finally { requests.delete(request); }
  }
  /* Reverify the opaque actor with session refreshes and before mutations.
     Concurrent refreshes share one request; display-only email is never used
     to decide whether drafts belong to this session. */
  function refreshSession() {
    if (state.signedOut) return Promise.resolve('redirected');
    if (!sessionRequest) {
      sessionRequest = verifySession().finally(function () { sessionRequest = null; });
    }
    return sessionRequest;
  }
  function sessionUnavailable(ctx) {
    if (current(ctx) && !state.signedOut) {
      /* A network failure cannot prove the cached actor still owns this
         session. Keep attributed disk drafts sealed until verification. */
      resetActor(null, true);
      csrf = null;
      state.me = null;
      renderIdentity({ name: 'Verifying session' });
      setListStatus('Could not verify your session. Reconnect and retry.', true);
      scheduleList(LIST_POLL_MS);
    }
    return 'error';
  }
  async function verifySession() {
    var ctx = context(false);
    sessionController = new AbortController();
    var options = { credentials: 'same-origin', cache: 'no-store', signal: sessionController.signal };
    try {
      var response = await fetch(CONV_API.me, options);
      checkCurrent(ctx);
      if (response.status === 401) { forgetSession(); globalThis.location.assign(CONV_API.login); return 'redirected'; }
      if (!response.ok) return sessionUnavailable(ctx);
      var me = await response.json();
      checkCurrent(ctx);
      if (!me || me.authenticated === false) { forgetSession(); globalThis.location.assign(CONV_API.login); return 'redirected'; }
      var responseParticipants = await fetch(CONV_API.participants, options);
      checkCurrent(ctx);
      if (responseParticipants.status === 401) { forgetSession(); globalThis.location.assign(CONV_API.login); return 'redirected'; }
      if (!responseParticipants.ok) return sessionUnavailable(ctx);
      var data = await responseParticipants.json();
      checkCurrent(ctx);
      var self = normalizeParticipant(data && data.self);
      if (!self) return sessionUnavailable(ctx);
      var changedActor = !!state.selfActor && state.selfActor !== self.actor;
      if (state.selfActor !== self.actor) resetActor(self.actor);
      applyParticipants(data);
      state.me = me;
      csrf = me.csrf_token;
      renderIdentity(me);
      if (changedActor) scheduleList(0);
      return 'ok';
    } catch (e) { return sessionUnavailable(ctx); }
  }

  /* ---- contract adapter: real M1 responses -> view model ---- */

  function normalizeParticipant(p) {
    if (!p || typeof p.actor !== 'string' || !p.actor) return null;
    return {
      actor: p.actor,
      kind: p.kind === 'agent' ? 'agent' : 'human',
      displayName: typeof p.display_name === 'string' && p.display_name ? p.display_name : p.actor
    };
  }
  function normalizeSummary(c) {
    if (!c || typeof c.id !== 'string') return null;
    return {
      id: c.id,
      kind: c.kind === 'group' ? 'group' : 'direct',
      title: titleFor(c),
      memberCount: typeof c.member_count === 'number' ? c.member_count : null,
      lastSeq: typeof c.last_seq === 'number' ? c.last_seq : 0,
      preview: typeof c.last_message_preview === 'string' ? c.last_message_preview : '',
      updatedAt: typeof c.updated_at === 'string' ? c.updated_at : null,
      myReadSeq: typeof c.my_read_seq === 'number' ? c.my_read_seq : 0,
      unreadCount: typeof c.unread_count === 'number' ? c.unread_count : 0
    };
  }
  function normalizeDetail(data) {
    if (!data || typeof data.id !== 'string') return null;
    var members = Array.isArray(data.members)
      ? data.members.map(function (m) {
          return {
            actor: String(m.actor),
            kind: m.kind === 'agent' ? 'agent' : 'human',
            role: typeof m.role === 'string' ? m.role : 'member'
          };
        })
      : [];
    return {
      id: data.id,
      kind: data.kind === 'group' ? 'group' : 'direct',
      title: titleFor(data),
      memberCount: typeof data.member_count === 'number' ? data.member_count : members.length,
      lastSeq: typeof data.last_seq === 'number' ? data.last_seq : 0,
      preview: typeof data.last_message_preview === 'string' ? data.last_message_preview : '',
      updatedAt: typeof data.updated_at === 'string' ? data.updated_at : null,
      myReadSeq: typeof data.my_read_seq === 'number' ? data.my_read_seq : 0,
      unreadCount: typeof data.unread_count === 'number' ? data.unread_count : 0,
      archived: !!data.archived_at,
      members: members
    };
  }
  function normalizeMessage(m) {
    if (!m || typeof m.id !== 'string' || typeof m.seq !== 'number') return null;
    return {
      id: m.id,
      seq: m.seq,
      senderActor: typeof m.sender_actor === 'string' ? m.sender_actor : '',
      senderKind: m.sender_kind === 'agent' ? 'agent' : 'human',
      kind: typeof m.kind === 'string' ? m.kind : 'message',
      body: typeof m.body === 'string' ? m.body : '',
      createdAt: typeof m.created_at === 'string' ? m.created_at : null
    };
  }
  function normalizeMessagesPage(data) {
    data = data || {};
    var messages = (Array.isArray(data.messages) ? data.messages : [])
      .map(normalizeMessage)
      .filter(Boolean)
      .sort(function (a, b) { return a.seq - b.seq; });
    return {
      messages: messages,
      selfActor: typeof data.self_actor === 'string' ? data.self_actor : null,
      lastSeq: typeof data.last_seq === 'number' ? data.last_seq : 0,
      myReadSeq: typeof data.my_read_seq === 'number' ? data.my_read_seq : 0,
      hasMoreBefore: !!data.has_more_before,
      hasMoreAfter: !!data.has_more_after,
      nextAfterSeq: typeof data.next_after_seq === 'number' ? data.next_after_seq : null,
      prevBeforeSeq: typeof data.prev_before_seq === 'number' ? data.prev_before_seq : null
    };
  }
  function normalizeCreatedMessage(data) {
    /* Send responses wrap the message at top level and under "message". */
    return normalizeMessage((data && data.message) || data);
  }
  /* Titles come from the server (title is required on create, direct
     included). The fallback only covers responses created before that rule. */
  function titleFor(conversation) {
    if (typeof conversation.title === 'string' && conversation.title) return conversation.title;
    var members = Array.isArray(conversation.members) ? conversation.members : [];
    var others = members.filter(function (m) { return m.actor !== state.selfActor; });
    if (conversation.kind === 'direct' && others[0]) return state.directory[others[0].actor] || others[0].actor || 'Direct conversation';
    if (others.length) return others.map(function (m) { return state.directory[m.actor] || m.actor; }).join(', ');
    if (typeof conversation.member_count === 'number' && conversation.kind === 'group') return 'Group conversation';
    return 'Conversation';
  }
  function displayLabel(actor) {
    return state.directory[actor] || actor || 'Unknown';
  }

  /* ---- identity chrome ---- */

  function initialsFor(name) {
    var parts = String(name || '').trim().split(/\s+/).filter(Boolean);
    if (!parts.length) return '?';
    return parts.slice(0, 2).map(function (p) { return p[0].toUpperCase(); }).join('');
  }
  function renderIdentity(me) {
    var name = me.name || me.email || 'Signed in';
    byId('account-name').textContent = name;
    byId('menu-name').textContent = name;
    byId('menu-email').textContent = me.email || '';
    var avatar = byId('account-avatar');
    avatar.replaceChildren(el('span', 'dk-avatar__initials', initialsFor(name)));
    var trigger = document.querySelector('.dk-account-trigger');
    if (trigger) trigger.setAttribute('aria-label', 'Account, ' + name);
  }

  /* ---- participants (verified identities) ---- */

  async function loadParticipants() {
    if (state.participantsLoaded) return;
    await refreshSession();
  }
  function applyParticipants(data) {
    var chosen = new Set(state.participants.filter(function (p) { return p.chosen; }).map(function (p) { return p.actor; }));
    state.directory = {};
    state.participants = [];
    var raw = data && Array.isArray(data.participants) ? data.participants : [];
    var self = data && data.self ? normalizeParticipant(data.self) : null;
    if (self) { state.directory[self.actor] = self.displayName; }
    for (var entry of raw) {
      var participant = normalizeParticipant(entry);
      if (!participant) continue;
      participant.chosen = chosen.has(participant.actor);
      state.directory[participant.actor] = participant.displayName;
      if (participant.actor !== state.selfActor) state.participants.push(participant);
    }
    state.participantsLoaded = true;
    var newBtn = byId('new-conversation');
    if (newBtn) newBtn.disabled = !state.selfActor;
  }

  /* ---- shared formatting ---- */

  function formatTime(iso) {
    var value = Date.parse(iso);
    if (!Number.isFinite(value)) return '';
    return new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
  function formatDay(iso) {
    var value = Date.parse(iso);
    if (!Number.isFinite(value)) return '';
    var date = new Date(value);
    var today = new Date();
    var yesterday = new Date(Date.now() - 86400000);
    var same = function (a, b) { return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate(); };
    if (same(date, today)) return 'Today';
    if (same(date, yesterday)) return 'Yesterday';
    return date.toLocaleDateString([], { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
  }
  function formatListTime(iso) {
    var value = Date.parse(iso);
    if (!Number.isFinite(value)) return '';
    var date = new Date(value);
    var now = new Date();
    if (date.getFullYear() === now.getFullYear() && date.getMonth() === now.getMonth() && date.getDate() === now.getDate()) return formatTime(iso);
    return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
  }

  /* ---- conversations list ---- */

  function setListStatus(text, isError) {
    byId('conv-state').textContent = text;
    var box = byId('conv-list-state');
    if (!isError) return;
    var note = el('div', 'dk-callout dk-callout--danger');
    var body = el('div', 'dk-callout__body');
    body.append(el('div', 'dk-callout__title', 'Could not load conversations.'));
    body.append(el('p', null, text));
    var actions = el('div', 'dk-callout__actions');
    var retry = el('button', 'dk-button dk-button--secondary dk-button--sm', 'Retry');
    retry.type = 'button';
    retry.addEventListener('click', function () { refreshConversations(); });
    actions.append(retry);
    body.append(actions);
    note.append(body);
    box.hidden = false;
    box.replaceChildren(note);
  }

  function emptyNode(compact, title, description) {
    var box = el('div', 'dk-empty' + (compact ? ' dk-empty--compact' : ''));
    box.append(el('p', 'dk-empty__title', title));
    box.append(el('p', 'dk-empty__desc', description));
    return box;
  }

  function renderConversations() {
    var list = byId('conv-list');
    var stateBox = byId('conv-list-state');
    stateBox.replaceChildren();
    list.replaceChildren();
    for (var conversation of state.conversations) list.append(conversationRow(conversation, conversation.id === state.currentId));
    byId('conv-count').textContent = state.conversations.length ? String(state.conversations.length) : '';
    var unread = state.conversations.reduce(function (sum, c) { return sum + (c.unreadCount || 0); }, 0);
    if (globalThis.dakitShell) globalThis.dakitShell.setCount(byId('unread-total'), unread);
    if (!state.conversations.length) {
      stateBox.hidden = false;
      stateBox.append(emptyNode(false, 'No conversations yet', 'Start one with New conversation; choose from participants registered with the hub.'));
    } else {
      stateBox.hidden = true;
    }
  }

  function conversationRow(conversation, selected) {
    var item = el('li');
    var link = el('a', 'dk-split__item');
    link.href = '/conversations.html?conversation=' + encodeURIComponent(conversation.id);
    if (selected) link.setAttribute('aria-current', 'true');
    var name = el('span', 'dk-split__name', conversation.title);
    link.append(name);
    var meta = el('span', 'dk-split__meta');
    if (conversation.kind === 'group') meta.append(el('span', 'dk-badge', 'Group'));
    if (conversation.preview) meta.append(el('span', 'conv-preview', previewText(conversation.preview)));
    if (conversation.updatedAt) {
      var time = el('span', 'dk-mono', formatListTime(conversation.updatedAt));
      time.setAttribute('aria-hidden', 'true');
      meta.append(time);
    }
    if (conversation.unreadCount > 0) {
      var badge = el('span', 'dk-count', conversation.unreadCount > 99 ? '99+' : String(conversation.unreadCount));
      badge.setAttribute('aria-label', conversation.unreadCount + ' unread');
      meta.append(badge);
    }
    link.append(meta);
    item.append(link);
    return item;
  }
  function previewText(text) {
    var body = String(text || '').replace(/\s+/g, ' ').trim();
    return body.length > 80 ? body.slice(0, 79) + '…' : body;
  }

  async function refreshConversations() {
    var previousActor = state.selfActor;
    if (await refreshSession() !== 'ok') return;
    var ctx = context(false);
    try {
      var data = await api(CONV_API.conversations);
      state.conversations = ((data && data.conversations) || []).map(normalizeSummary).filter(Boolean);
      state.listBackoff = 0;
      renderConversations();
      setListStatus(state.conversations.length ? 'Updated ' + new Date().toLocaleTimeString() : 'No conversations yet.');
      scheduleList(LIST_POLL_MS);
    } catch (error) {
      if (!current(ctx) || error && (error.kind === 'auth' || error.kind === 'stale')) return;
      if (error && error.kind === 'forbidden') { setListStatus(error.message, true); return; }
      state.listBackoff = state.listBackoff ? Math.min(state.listBackoff * 2, POLL_MAX_MS) : LIST_POLL_MS;
      renderConversations();
      setListStatus(error.message || 'Hub request failed.', true);
      announce('Conversation list failed to load.');
      scheduleList(state.listBackoff);
    }
    renderListSelection();
    if ((previousActor && previousActor !== state.selfActor) ||
        (state.currentId && !state.details[state.currentId] && !threadState(state.currentId).forbidden)) route();
  }
  function scheduleList(delay) {
    if (state.listTimer) clearTimeout(state.listTimer);
    state.listTimer = setTimeout(function () { refreshConversations(); }, delay);
  }

  /* ---- thread ---- */

  function threadState(id) {
    if (!state.threads[id]) {
      state.threads[id] = { messages: [], oldestSeq: null, newestSeq: null, lastSeq: 0, myReadSeq: 0, hasMoreBefore: false, selfActor: null, loaded: false, loading: false, forbidden: false };
    }
    return state.threads[id];
  }
  function outboxEntries(id) {
    if (!state.selfActor) return [];
    var key = outboxKey(state.selfActor, id);
    if (!state.outbox[key]) {
      var saved = [];
      try { saved = JSON.parse(globalThis.localStorage.getItem(key) || '[]'); } catch (e) { saved = []; }
      state.outbox[key] = Array.isArray(saved)
        ? saved.filter(function (entry) {
            return entry && entry.actor === state.selfActor && entry.conversationId === id &&
              typeof entry.body === 'string' && entry.body && typeof entry.idempotencyKey === 'string' && entry.idempotencyKey &&
              typeof entry.clientId === 'string' && entry.clientId;
          })
            .map(function (entry) {
              /* nothing can be in flight after a reload: restored
                 "sending" entries are failed drafts waiting for retry */
              if (entry.pending !== 'failed') { entry.pending = 'failed'; entry.error = entry.error || 'Sending was interrupted. Use Retry.'; }
              return entry;
            })
        : [];
    }
    return state.outbox[key];
  }
  /* JSON tuples are injective even when opaque actors/ids contain colons,
     percent signs or quotes. Legacy unscoped data cannot be attributed. */
  function outboxKey(actor, id) { return 'conv-outbox:v2:' + JSON.stringify([actor, id]); }
  function purgeOutboxes(actor, keepScopedDisk) {
    try {
      for (var key of Object.keys(globalThis.localStorage)) {
        if (!key.startsWith('conv-outbox:')) continue;
        var parts = null;
        try { if (key.startsWith('conv-outbox:v2:')) parts = JSON.parse(key.slice('conv-outbox:v2:'.length)); } catch (e) { /* unattributable */ }
        if (!Array.isArray(parts) || parts.length !== 2 || typeof parts[0] !== 'string' || !parts[0] || typeof parts[1] !== 'string' ||
            key !== outboxKey(parts[0], parts[1]) || !keepScopedDisk && (!actor || parts[0] !== actor)) {
          globalThis.localStorage.removeItem(key);
        }
      }
    } catch (e) { /* storage blocked */ }
  }
  function saveOutbox(id) {
    if (!state.selfActor) return;
    try { globalThis.localStorage.setItem(outboxKey(state.selfActor, id), JSON.stringify(outboxEntries(id))); } catch (e) { /* storage blocked */ }
  }
  function allMessages(id) {
    return threadState(id).messages.concat(outboxEntries(id));
  }

  async function openConversation(id, options) {
    options = options || {};
    changeRoute(id);
    var ctx = context(true);
    if (!id) { render(); return; }
    outboxEntries(id);
    if (globalThis.dakitSplit) globalThis.dakitSplit.show(byId('conv-split'), 'detail', { focus: false });
    render();
    await loadDetail(id);
    if (!current(ctx) || state.currentId !== id || !state.selfActor || threadState(id).forbidden) return;
    var thread = threadState(id);
    if (!thread.loaded && !thread.loading) await refreshThread(id, { initial: true });
    else { scrollToLatest(); markReadIfNeeded(); }
  }

  async function loadDetail(id) {
    var ctx = context(true);
    try {
      var data = await api(CONV_API.conversation(id), { routeBound: true });
      state.details[id] = normalizeDetail(data);
      delete state.detailErrors[id];
      if (state.currentId === id) render();
    } catch (error) {
      if (!current(ctx) || error && (error.kind === 'auth' || error.kind === 'stale')) return;
      state.detailErrors[id] = error.message || 'Could not load conversation details.';
      if (error && error.kind === 'forbidden') denyThread(id, error.message);
    }
  }

  async function refreshThread(id, options) {
    options = options || {};
    var thread = threadState(id);
    if ((thread.loading && current(thread.loadContext)) || thread.forbidden || !state.selfActor || state.currentId !== id) return;
    var ctx = context(true);
    thread.loading = true;
    thread.loadContext = ctx;
    try {
      var query;
      if (options.before && thread.oldestSeq !== null) query = '?limit=' + CONV_API.historyLimit + '&before_seq=' + encodeURIComponent(thread.oldestSeq);
      else if (!options.initial && thread.loaded && thread.newestSeq !== null) query = '?limit=' + CONV_API.historyLimit + '&after_seq=' + encodeURIComponent(thread.newestSeq);
      else query = '?limit=' + CONV_API.historyLimit;
      var page = normalizeMessagesPage(await api(CONV_API.messages(id) + query, { routeBound: true }));
      thread.selfActor = page.selfActor || thread.selfActor;
      var known = new Set(thread.messages.map(function (m) { return m.id; }));
      var wasLoaded = thread.loaded;
      var fresh;
      if (options.before) {
        fresh = page.messages.filter(function (m) { return !known.has(m.id); });
        thread.messages = fresh.concat(thread.messages);
        thread.hasMoreBefore = page.hasMoreBefore;
      } else {
        fresh = page.messages.filter(function (m) { return !known.has(m.id); });
        thread.messages = thread.messages.concat(fresh);
        if (!options.initial && fresh.length) announceIncoming(fresh, id);
        if (!wasLoaded) thread.hasMoreBefore = page.hasMoreBefore;
      }
      thread.messages.sort(function (a, b) { return a.seq - b.seq; });
      thread.oldestSeq = thread.messages.length ? thread.messages[0].seq : null;
      thread.newestSeq = thread.messages.length ? thread.messages[thread.messages.length - 1].seq : null;
      thread.lastSeq = Math.max(thread.lastSeq, page.lastSeq);
      if (page.myReadSeq > thread.myReadSeq) thread.myReadSeq = page.myReadSeq;
      thread.forbidden = false;
      thread.loaded = true;
      state.threadBackoff = 0;
      if (state.currentId === id) {
        if (!wasLoaded || fresh.length || !thread.messages.length) render();
        if (options.initial || options.stickToBottom || nearBottom()) scrollToLatest();
        markReadIfNeeded();
      }
      scheduleThread(THREAD_POLL_MS);
    } catch (error) {
      if (!current(ctx) || error && (error.kind === 'auth' || error.kind === 'stale')) return;
      if (error && error.kind === 'forbidden') {
        /* Membership failure: stop the poll loop, keep drafts, show access state. */
        denyThread(id, error.message);
        return;
      }
      state.threadBackoff = state.threadBackoff ? Math.min(state.threadBackoff * 2, POLL_MAX_MS) : THREAD_POLL_MS;
      if (state.currentId === id) { render(); setThreadError(error.message || 'Could not load messages.'); }
      scheduleThread(state.threadBackoff);
    } finally {
      if (thread.loadContext === ctx) thread.loading = false;
    }
  }
  function denyThread(id, message) {
    var thread = threadState(id);
    thread.forbidden = true;
    thread.error = message;
    clearTimeout(state.threadTimer);
    if (state.currentId === id) render();
    announce('This conversation is not accessible.');
  }
  function scheduleThread(delay) {
    if (state.threadTimer) clearTimeout(state.threadTimer);
    state.threadTimer = setTimeout(function () {
      if (state.currentId && document.visibilityState === 'visible' && !threadState(state.currentId).forbidden) refreshThread(state.currentId, {});
    }, delay);
  }
  function announceIncoming(fresh, id) {
    var self = threadState(id).selfActor || state.selfActor;
    var others = fresh.filter(function (m) { return m.senderActor !== self; });
    var conversation = state.conversations.find(function (c) { return c.id === id; });
    if (others.length && conversation) {
      announce(others.length + ' new message' + (others.length > 1 ? 's' : '') + ' in ' + conversation.title);
    }
  }

  async function loadOlder() {
    var id = state.currentId;
    if (!id) return;
    var thread = threadState(id);
    if (!thread.messages.length || thread.oldestSeq === null) return;
    var scroller = byId('thread-scroll');
    var previousHeight = scroller ? scroller.scrollHeight : 0;
    await refreshThread(id, { before: true });
    if (scroller && !nearBottom()) scroller.scrollTop = scroller.scrollHeight - previousHeight;
  }

  function setThreadError(message) {
    var box = byId('thread-error');
    if (!box) return;
    box.hidden = false;
    box.replaceChildren();
    var note = el('div', 'dk-callout dk-callout--danger');
    var body = el('div', 'dk-callout__body');
    body.append(el('div', 'dk-callout__title', 'Could not load messages.'));
    body.append(el('p', null, message));
    if (!threadState(state.currentId).forbidden) {
      var actions = el('div', 'dk-callout__actions');
      var retry = el('button', 'dk-button dk-button--secondary dk-button--sm', 'Retry');
      retry.type = 'button';
      retry.addEventListener('click', function () {
        box.hidden = true;
        refreshThread(state.currentId, { initial: !threadState(state.currentId).loaded });
      });
      actions.append(retry);
      body.append(actions);
    }
    note.append(body);
    box.append(note);
  }

  /* ---- rendering (draft text, focus and scroll survive re-renders) ---- */

  function composerSnapshot() {
    var field = byId('composer-field');
    if (!field) return null;
    return {
      actor: state.selfActor,
      conversationId: state.currentId,
      value: field.value,
      focused: document.activeElement === field,
      selectionStart: field.selectionStart,
      selectionEnd: field.selectionEnd,
      scroll: byId('thread-scroll') ? byId('thread-scroll').scrollTop : 0,
      pinned: nearBottom()
    };
  }
  function restoreComposer(snapshot) {
    if (!snapshot || !state.selfActor || snapshot.actor !== state.selfActor || snapshot.conversationId !== state.currentId) return;
    var field = byId('composer-field');
    var scroller = byId('thread-scroll');
    if (field) {
      field.value = snapshot.value;
      if (snapshot.focused) {
        field.focus();
        try { field.setSelectionRange(snapshot.selectionStart, snapshot.selectionEnd); } catch (e) { /* detached */ }
      }
    }
    if (scroller) scroller.scrollTop = snapshot.pinned ? scroller.scrollHeight : snapshot.scroll;
  }
  function nearBottom() {
    var scroller = byId('thread-scroll');
    if (!scroller) return true;
    return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
  }
  function scrollToLatest() {
    var scroller = byId('thread-scroll');
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
  }

  function render() {
    var snapshot = composerSnapshot();
    renderListSelection();
    renderDetail();
    restoreComposer(snapshot);
  }

  function renderListSelection() {
    var items = byId('conv-list').querySelectorAll('.dk-split__item');
    for (var item of items) {
      var params = new URLSearchParams((item.getAttribute('href') || '').split('?')[1] || '');
      if (params.get('conversation') === state.currentId) item.setAttribute('aria-current', 'true');
      else item.removeAttribute('aria-current');
    }
  }

  function detailFor(id) {
    return state.details[id] || state.conversations.find(function (c) { return c.id === id; }) || null;
  }

  function renderDetail() {
    var detail = byId('conv-detail');
    detail.replaceChildren();
    var id = state.currentId;
    if (!id) {
      var empty = el('div', 'dk-split__empty');
      empty.append(emptyNode(false, 'Select a conversation', 'Choose a conversation on the left, or start one with New conversation.'));
      detail.append(empty);
      return;
    }
    var conversation = detailFor(id) || { id: id, title: 'Conversation', members: [] };
    detail.append(detailHead(id, conversation));
    var errorBox = el('div', 'conv-list-state');
    errorBox.id = 'thread-error';
    errorBox.hidden = true;
    detail.append(errorBox);
    detail.append(followThroughNode(id));
    detail.append(threadNode(id));
    detail.append(composerNode(conversation));
    if (threadState(id).forbidden) setThreadError(threadState(id).error || 'You do not have access to this conversation.');
    loadFollowThrough(id);
  }

  function detailHead(id, conversation) {
    var head = el('div', 'conv-detail-head');
    var back = el('a', 'dk-split__back', 'Conversations');
    back.href = '/conversations.html';
    back.setAttribute('aria-label', 'Back to Conversations');
    back.append(svgIcon('m15 18-6-6 6-6'));
    head.append(back);
    head.append(el('h2', 'conv-detail-head__title', conversation.title || 'Conversation'));
    var detail = state.details[id];
    var members = detail && detail.members
      ? detail.members.map(function (m) {
          return (m.actor === state.selfActor ? 'You' : displayLabel(m.actor)) + ' (' + m.kind + ')';
        })
      : [];
    head.append(el('p', 'conv-detail-head__meta', members.length ? 'Members: ' + members.join(', ') : 'Members are not available yet.'));
    return head;
  }

  async function loadFollowThrough(id) {
    if (!id || !state.selfActor) return;
    var ctx = context(true);
    try {
      var data = await api(CONV_API.followThrough(id), { routeBound: true, context: ctx });
      checkCurrent(ctx);
      var links = (data && (data.follow_through || data.links)) || [];
      state.followThrough[id] = { loaded: true, error: null, links: links };
      renderFollowThrough(id);
    } catch (err) {
      if (!current(ctx) || (err && err.kind === 'stale')) return;
      state.followThrough[id] = { loaded: true, error: null, links: [] };
      renderFollowThrough(id);
    }
  }

  function renderFollowThrough(id) {
    if (id !== state.currentId) return;
    var body = byId('conv-ft-body');
    if (!body) return;
    renderFollowThroughContent(body, id);
  }

  function followThroughNode(id) {
    var wrap = el('section', 'conv-ft');
    wrap.id = 'conv-ft';
    wrap.setAttribute('aria-label', 'Follow-through and Linked Tasks');

    var head = el('div', 'conv-ft__head');
    head.append(el('h3', 'conv-ft__title', 'Follow-through & Linked Tasks'));

    var attachBtn = el('button', 'dk-button dk-button--sm dk-button--secondary', 'Attach link');
    attachBtn.type = 'button';
    head.append(attachBtn);
    wrap.append(head);

    var formBox = el('div', 'conv-ft__form');
    formBox.hidden = true;

    var kindSelect = el('select', 'dk-select');
    var opt1 = el('option', null, 'Intake Task');
    opt1.value = 'intake_task';
    var opt2 = el('option', null, 'Routing Job');
    opt2.value = 'routing_job';
    var opt3 = el('option', null, 'Fleet Job');
    opt3.value = 'fleet_job';
    kindSelect.append(opt1, opt2, opt3);

    var refInput = el('input', 'dk-input');
    refInput.type = 'text';
    refInput.placeholder = 'Task UUID or Job ID';
    refInput.setAttribute('aria-label', 'Target reference identifier');

    var submitBtn = el('button', 'dk-button dk-button--sm', 'Attach');
    submitBtn.type = 'button';

    formBox.append(kindSelect, refInput, submitBtn);
    wrap.append(formBox);

    var formOpen = false;
    attachBtn.addEventListener('click', function () {
      formOpen = !formOpen;
      formBox.hidden = !formOpen;
      attachBtn.textContent = formOpen ? 'Cancel' : 'Attach link';
      if (formOpen) refInput.focus();
    });

    submitBtn.addEventListener('click', async function () {
      var refVal = refInput.value.trim();
      if (!refVal) return;
      submitBtn.disabled = true;
      try {
        await api(CONV_API.links(id), {
          method: 'POST',
          body: {
            kind: kindSelect.value,
            ref: refVal,
            idempotency_key: 'link-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8)
          }
        });
        refInput.value = '';
        formOpen = false;
        formBox.hidden = true;
        attachBtn.textContent = 'Attach link';
        if (state.followThrough[id]) delete state.followThrough[id];
        loadFollowThrough(id);
      } catch (err) {
        announce(err && err.message ? err.message : 'Failed to attach link');
      } finally {
        submitBtn.disabled = false;
      }
    });

    var body = el('div', 'conv-ft__body');
    body.id = 'conv-ft-body';
    wrap.append(body);
    renderFollowThroughContent(body, id);
    return wrap;
  }

  function renderFollowThroughContent(container, id) {
    container.replaceChildren();
    var ft = state.followThrough[id];
    if (!ft) {
      container.append(el('p', 'conv-ft__empty', 'Loading follow-through facts…'));
      return;
    }
    if (ft.error) {
      container.append(el('p', 'conv-ft__empty', ft.error));
      return;
    }
    if (!ft.links || ft.links.length === 0) {
      container.append(el('p', 'conv-ft__empty', 'No linked tasks or jobs attached to this conversation.'));
      return;
    }

    var list = el('div', 'conv-ft-list');
    for (var i = 0; i < ft.links.length; i++) {
      list.append(linkCardNode(id, ft.links[i]));
    }
    container.append(list);
  }

  function linkCardNode(convId, item) {
    var card = el('div', 'conv-ft-item');

    var top = el('div', 'conv-ft-item__top');
    var kindName = item.kind === 'intake_task' ? 'Intake Task' : (item.kind === 'routing_job' ? 'Routing Job' : item.kind);
    top.append(el('span', 'dk-badge', kindName));
    top.append(el('span', 'conv-ft-item__ref', item.ref));

    var target = item.target || (item.follow_through && item.follow_through.target);
    if (!target || !target.available) {
      top.append(el('span', 'dk-badge dk-badge--warning', 'Opaque ACL / unavailable'));
    } else {
      top.append(el('span', 'dk-badge dk-badge--info', target.canonical_status || 'available'));
    }

    var summary = (item.follow_through && item.follow_through.summary) || item.summary || {};
    if (summary.complete) {
      top.append(el('span', 'dk-badge dk-badge--success', 'Complete'));
    } else if (summary.blocked) {
      top.append(el('span', 'dk-badge dk-badge--danger', 'Blocked'));
    } else if (item.overdue_obligation || (item.follow_through && item.follow_through.overdue_obligation)) {
      top.append(el('span', 'dk-badge dk-badge--warning', 'Overdue obligation'));
    } else if (summary.ready) {
      top.append(el('span', 'dk-badge dk-badge--info', 'Ready'));
    } else {
      top.append(el('span', 'dk-badge', 'In progress'));
    }

    var linkId = item.id || item.link_id;
    if (linkId) {
      var actBox = el('div', 'conv-ft-item__actions');
      var removeBtn = el('button', 'dk-button dk-button--sm dk-button--secondary', 'Remove');
      removeBtn.type = 'button';
      removeBtn.addEventListener('click', async function () {
        removeBtn.disabled = true;
        try {
          await api(CONV_API.link(convId, linkId), { method: 'DELETE' });
          if (state.followThrough[convId]) delete state.followThrough[convId];
          loadFollowThrough(convId);
        } catch (err) {
          announce(err && err.message ? err.message : 'Failed to remove link');
          removeBtn.disabled = false;
        }
      });
      actBox.append(removeBtn);
      top.append(actBox);
    }
    card.append(top);

    var facts = (item.follow_through && item.follow_through.facts) || item.facts;
    if (facts) {
      var grid = el('div', 'conv-ft-grid');

      grid.append(factItemNode('Owner Acceptance', facts.owner_acceptance, function (f) {
        return f.state === 'known' ? 'Accepted' : (f.state === 'refused' ? 'Refused' : 'Unknown');
      }));

      grid.append(factItemNode('First Action', facts.first_action, function (f) {
        return f.state === 'known' ? 'Recorded' : 'Pending';
      }));

      grid.append(factItemNode('Due Obligations', facts.due_obligation, function (f) {
        if (f.state === 'overdue') return 'Overdue';
        if (f.state === 'open') return 'Open';
        if (f.state === 'answered' || f.state === 'proceeded') return 'Answered';
        return f.state || 'None';
      }));

      grid.append(factItemNode('Independent Review', facts.independent_review, function (f) {
        if (f.state === 'known') return 'Approved';
        if (f.state === 'refused') return 'Changes Requested';
        return 'Pending';
      }));

      grid.append(factItemNode('Accepted Outcome', facts.accepted_outcome, function (f) {
        if (f.state === 'known') return 'Accepted';
        if (f.state === 'refused') return 'Rejected';
        return 'Pending';
      }));

      grid.append(factItemNode('Human Delivery', facts.human_delivery, function (f) {
        return f.state === 'known' ? 'Delivered' : 'Pending';
      }));

      card.append(grid);
    }

    return card;
  }

  function factItemNode(title, fact, labelFn) {
    var node = el('div', 'conv-ft-fact');
    node.append(el('div', 'conv-ft-fact__title', title));

    if (!fact) {
      node.append(el('span', 'dk-badge', 'Unknown'));
      return node;
    }

    var stateName = fact.state || 'unknown';
    var variant = '';
    if (stateName === 'known' || stateName === 'answered' || stateName === 'proceeded') variant = 'success';
    else if (stateName === 'open' || stateName === 'overdue-reconciled') variant = 'warning';
    else if (stateName === 'refused' || stateName === 'overdue') variant = 'danger';

    var label = labelFn ? labelFn(fact) : stateName;
    node.append(el('span', 'dk-badge' + (variant ? ' dk-badge--' + variant : ''), label));

    if (fact.source_identity) {
      node.append(el('span', 'conv-ft-fact__source', fact.source_identity));
    }
    if (fact.authority && fact.authority !== 'none') {
      node.append(el('span', 'conv-ft-fact__detail', fact.authority));
    }
    if (fact.observation) {
      if (fact.observation.source_sha) {
        node.append(el('span', 'conv-ft-fact__detail', 'SHA: ' + String(fact.observation.source_sha).slice(0, 8)));
      } else if (fact.observation.outcome_ref) {
        node.append(el('span', 'conv-ft-fact__detail', String(fact.observation.outcome_ref).slice(0, 16)));
      } else if (fact.observation.first_action_at) {
        node.append(el('span', 'conv-ft-fact__detail', String(fact.observation.first_action_at).slice(11, 19)));
      }
    }
    return node;
  }

  function threadNode(id) {
    var wrap = el('div', 'conv-thread');
    var scroller = el('div', 'conv-thread__scroll');
    scroller.id = 'thread-scroll';
    var thread = threadState(id);
    if (!thread.loaded) {
      var loading = el('div', 'conv-thread__top');
      for (var i = 0; i < 3; i++) loading.append(el('div', 'dk-skeleton', ''));
      scroller.append(loading);
      wrap.append(scroller);
      return wrap;
    }
    if (thread.hasMoreBefore && thread.messages.length) {
      var top = el('div', 'conv-thread__top');
      var older = el('button', 'dk-button dk-button--secondary dk-button--sm', 'Load older messages');
      older.type = 'button';
      older.addEventListener('click', function () { older.disabled = true; loadOlder().finally(function () { older.disabled = false; }); });
      top.append(older);
      scroller.append(top);
    }
    if (!thread.messages.length && !outboxEntries(id).length) {
      scroller.append(emptyNode(true, 'No messages yet', 'Say hello — the first message starts this conversation.'));
    }
    var lastDay = null;
    for (var message of allMessages(id)) {
      var day = message.createdAt ? formatDay(message.createdAt) : '';
      if (day && day !== lastDay) { scroller.append(el('p', 'conv-day', day)); lastDay = day; }
      scroller.append(messageNode(id, message));
    }
    wrap.append(scroller);
    return wrap;
  }

  function isSelfMessage(id, message) {
    var self = (threadState(id) && threadState(id).selfActor) || state.selfActor;
    return !!self && message.senderActor === self;
  }
  function messageNode(id, message) {
    var pending = !!message.pending;
    var self = pending || isSelfMessage(id, message);
    var row = el('article', 'msg' + (self ? ' msg--self' : ''));
    var side = el('div', 'msg__side');
    var avatar = el('span', 'dk-avatar dk-avatar--sm');
    avatar.setAttribute('aria-hidden', 'true');
    avatar.append(el('span', 'dk-avatar__initials', initialsFor(pending ? (state.me ? (state.me.name || state.me.email) : 'You') : (self ? 'You' : displayLabel(message.senderActor)))));
    side.append(avatar);
    row.append(side);
    var main = el('div', 'msg__main');
    var meta = el('div', 'msg__meta');
    meta.append(el('span', 'msg__sender', pending || self ? 'You' : displayLabel(message.senderActor)));
    if (message.createdAt) {
      var time = el('time', 'msg__time', formatTime(message.createdAt));
      time.setAttribute('datetime', message.createdAt);
      meta.append(time);
    }
    main.append(meta);
    main.append(el('p', 'msg__body', message.body));
    var status = el('div', 'msg__status');
    if (message.pending === 'sending') {
      var spin = el('span', 'dk-spinner');
      spin.setAttribute('role', 'img');
      spin.setAttribute('aria-label', 'Sending');
      status.append(spin, el('span', 'dk-badge', 'Sending…'));
    } else if (message.pending === 'failed') {
      status.append(el('span', 'dk-badge dk-badge--danger', 'Failed'));
      if (message.error) status.append(el('span', 'msg__error', message.error));
      var retry = el('button', 'dk-button dk-button--secondary dk-button--sm', 'Retry');
      retry.type = 'button';
      retry.setAttribute('aria-label', 'Retry sending this message');
      retry.disabled = !state.selfActor || threadState(id).forbidden || state.sending;
      retry.addEventListener('click', function () { sendOutbox(id, message.clientId); });
      status.append(retry);
      var discard = el('button', 'dk-button dk-button--secondary dk-button--sm', 'Discard draft');
      discard.type = 'button';
      discard.addEventListener('click', function () {
        var entries = outboxEntries(id);
        var index = entries.findIndex(function (entry) { return entry.clientId === message.clientId; });
        if (index >= 0) { entries.splice(index, 1); saveOutbox(id); render(); }
      });
      status.append(discard);
    }
    if (status.childNodes.length) main.append(status);
    row.append(main);
    return row;
  }

  function composerNode(conversation) {
    var form = el('form', 'composer');
    form.id = 'composer';
    var row = el('div', 'composer__row');
    var field = el('textarea', 'dk-textarea composer__field');
    field.id = 'composer-field';
    field.placeholder = 'Write a message';
    field.setAttribute('aria-label', 'Write a message to ' + (conversation.title || 'this conversation'));
    field.setAttribute('rows', '2');
    var send = el('button', 'dk-button dk-button--primary composer__send', 'Send');
    send.type = 'submit';
    row.append(field, send);
    form.append(row);
    var hint = el('div', 'composer__foot');
    var kbd = el('kbd', 'dk-kbd', 'Enter');
    hint.append(kbd, document.createTextNode(' to send, '));
    var kbd2 = el('kbd', 'dk-kbd', 'Shift+Enter');
    hint.append(kbd2, document.createTextNode(' for a new line.'));
    form.append(hint);
    syncComposerAvailability(form);

    field.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        if (form.requestSubmit) form.requestSubmit(send); else send.click();
      }
    });
    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      var draftValue = field.value;
      var body = draftValue.replace(/\s+$/, '');
      if (!body || !canSend(conversation.id)) return;
      if (new TextEncoder().encode(body).byteLength > 16384) {
        field.setCustomValidity('Messages must be at most 16,384 UTF-8 bytes.');
        field.reportValidity();
        return;
      }
      var ctx = context(true);
      state.sending = true;
      syncComposerAvailability();
      var verified = await refreshSession();
      if (!current(ctx)) return;
      state.sending = false;
      syncComposerAvailability();
      if (verified !== 'ok' || !canSend(conversation.id)) return;
      var entry = {
        actor: state.selfActor,
        conversationId: conversation.id,
        clientId: 'pending-' + globalThis.crypto.randomUUID(),
        body: body,
        idempotencyKey: globalThis.crypto.randomUUID(),
        pending: 'sending',
        error: null,
        createdAt: new Date().toISOString()
      };
      outboxEntries(conversation.id).push(entry);
      saveOutbox(conversation.id);
      var activeField = byId('composer-field');
      if (activeField && activeField.value === draftValue) activeField.value = '';
      render();
      byId('composer-field').focus();
      sendOutbox(conversation.id, entry.clientId, ctx);
    });
    field.addEventListener('input', function () { field.setCustomValidity(''); });
    return form;
  }

  function canSend(id) {
    return !!state.selfActor && state.currentId === id && !threadState(id).forbidden &&
      !state.sending && !state.offline && globalThis.navigator.onLine;
  }

  function syncComposerAvailability(form) {
    form = form || byId('composer');
    if (!form) return;
    var send = form.querySelector('.composer__send');
    if (!send) return;
    var offline = state.offline || !globalThis.navigator.onLine;
    var blocked = !state.selfActor || threadState(state.currentId).forbidden;
    var disabled = offline || state.sending || blocked;
    send.disabled = disabled;
    form.querySelector('.composer__field').disabled = blocked;
    form.setAttribute('aria-disabled', disabled ? 'true' : 'false');
    var note = byId('composer-offline');
    if (offline && !note) {
      note = el('p', 'msg__error', 'You are offline. Messages wait as drafts until you reconnect.');
      note.id = 'composer-offline';
      form.append(note);
    } else if (!offline && note) note.remove();
  }

  /* ---- sending, retry, idempotency ----
     A retry reuses the exact stored key and payload, so the server answers
     with the same message instead of appending a duplicate. */

  async function sendOutbox(conversationId, clientId, verifiedContext) {
    if (!canSend(conversationId)) return;
    var ctx = verifiedContext || context(true);
    state.sending = true;
    syncComposerAvailability();
    if (!verifiedContext) {
      var verified = await refreshSession();
      if (!current(ctx)) return;
      if (verified !== 'ok' || threadState(conversationId).forbidden) {
        state.sending = false;
        syncComposerAvailability();
        return;
      }
    }
    var entries = outboxEntries(conversationId);
    var entry = entries.find(function (e) { return e.clientId === clientId; });
    if (!entry || entry.actor !== state.selfActor) { state.sending = false; syncComposerAvailability(); return; }
    entry.pending = 'sending';
    entry.error = null;
    render();
    try {
      var data = await api(CONV_API.messages(conversationId), {
        method: 'POST',
        routeBound: true,
        context: ctx,
        body: { body: entry.body, idempotency_key: entry.idempotencyKey }
      });
      var created = normalizeCreatedMessage(data);
      var index = entries.indexOf(entry);
      if (index >= 0) entries.splice(index, 1);
      saveOutbox(conversationId);
      var thread = threadState(conversationId);
      if (created && !thread.messages.some(function (m) { return m.id === created.id; })) {
        thread.messages.push(created);
        thread.messages.sort(function (a, b) { return a.seq - b.seq; });
        thread.newestSeq = thread.messages.length ? thread.messages[thread.messages.length - 1].seq : created.seq;
        thread.lastSeq = Math.max(thread.lastSeq, created.seq);
      }
      announce('Message sent.');
      if (state.currentId === conversationId) { render(); scrollToLatest(); markReadIfNeeded(); }
      scheduleThread(THREAD_POLL_MS);
    } catch (error) {
      if (!current(ctx) || error && (error.kind === 'auth' || error.kind === 'stale')) {
        /* A route abort leaves a persisted, interrupted draft for this actor.
           An auth reset has already purged it and must never recreate it. */
        if (ctx.auth === state.authGeneration) {
          entry.pending = 'failed';
          entry.error = 'Sending was interrupted. Use Retry.';
          saveOutbox(conversationId);
        }
        return;
      }
      entry.pending = 'failed';
      entry.error = (error && error.message) || 'Sending failed.';
      saveOutbox(conversationId); /* the failed state itself must survive a reload */
      announce('Sending failed. The draft is kept; use Retry.');
      if (error && error.kind === 'forbidden') denyThread(conversationId, error.message);
      else if (state.currentId === conversationId) render();
    }
    state.sending = false;
    if (state.currentId === conversationId) render();
  }

  /* ---- read position ----
     The server cursor advances with { seq: <rendered seq> } and answers
     200 JSON with the persisted cursor. Monotonic: older or equal seqs are
     preserved server-side, and the UI only ever posts a higher seq. */

  async function markReadIfNeeded() {
    var id = state.currentId;
    if (!id || document.visibilityState !== 'visible' || !document.hasFocus()) return;
    var thread = threadState(id);
    if (thread.forbidden || !state.selfActor) return;
    var ctx = context(true);
    var last = thread.messages[thread.messages.length - 1];
    if (!last || last.seq <= thread.myReadSeq) return;
    if (state.markedReadFor === id + ':' + last.seq) return;
    state.markedReadFor = id + ':' + last.seq;
    var conversation = state.conversations.find(function (c) { return c.id === id; });
    if (conversation && conversation.unreadCount) { conversation.unreadCount = 0; renderConversations(); }
    try {
      var result = await api(CONV_API.read(id), { method: 'POST', routeBound: true, body: { seq: last.seq } });
      thread.myReadSeq = result && typeof result.read_seq === 'number' ? Math.max(thread.myReadSeq, result.read_seq) : last.seq;
    } catch (error) {
      if (!current(ctx) || error && (error.kind === 'auth' || error.kind === 'stale')) return;
      if (error && error.kind === 'forbidden') denyThread(id, error.message);
      state.markedReadFor = null; /* retried for the next render */
    }
  }

  /* ---- new conversation dialog ----
     title is required by the contract for every kind (direct included): a
     direct title is derived from the chosen participant's display name;
     groups ask for one. Members are canonical actor strings from the
     verified participants list — never typed or fabricated identifiers. */

  function newCreateKey() { return 'create-' + globalThis.crypto.randomUUID(); }

  function wireNewConversation() {
    var button = byId('new-conversation');
    var dialog = byId('new-conversation-dialog');
    var form = byId('new-conversation-form');
    var list = byId('identity-list');
    var filter = byId('identity-filter');
    var errorBox = byId('new-conversation-error');

    if (button) button.disabled = !state.selfActor || !state.participantsLoaded;

    button.addEventListener('click', async function () {
      if (button.disabled || !state.selfActor || !state.participantsLoaded) return;
      errorBox.hidden = true;
      errorBox.textContent = '';
      form.reset();
      byId('group-title-field').hidden = true;
      state.createKey = newCreateKey();
      state.createFingerprint = null;
      for (var participant of state.participants) participant.chosen = false;
      renderParticipantChoices('');
      dialog.showModal();
      if (!state.participantsLoaded) {
        await loadParticipants();
        renderParticipantChoices(filter.value);
        if (!state.participantsLoaded) {
          errorBox.textContent = 'Registered participants are not available right now.';
          errorBox.hidden = false;
        }
      }
    });
    byId('new-conversation-cancel').addEventListener('click', function () { dialog.close(); });
    form.addEventListener('submit', async function (event) {
      if (!event.submitter || event.submitter.id !== 'new-conversation-create') return;
      event.preventDefault();
      var kind = form.elements['conv-kind'].value;
      var chosen = state.participants.filter(function (participant) { return participant.chosen; });
      var title = byId('group-title').value.trim();
      if (kind === 'direct') {
        if (chosen.length !== 1) return showFormError('Choose exactly one participant for a direct conversation.');
        title = title || chosen[0].displayName;
      } else if (chosen.length < 1 || !title) {
        return showFormError('Groups need a name and at least one other participant.');
      }
      var payload = { kind: kind, title: title, members: chosen.map(function (p) { return p.actor; }).sort() };
      var fingerprint = JSON.stringify(payload);
      if (state.createFingerprint !== fingerprint || !state.createKey) state.createKey = newCreateKey();
      state.createFingerprint = fingerprint;
      payload.idempotency_key = state.createKey;
      var ctx = context(true);
      errorBox.hidden = true;
      var createButton = byId('new-conversation-create');
      createButton.disabled = true;
      try {
        if (await refreshSession() !== 'ok') throw { message: 'Your session could not be verified. Try again.' };
        checkCurrent(ctx);
        if (!state.selfActor) return;
        var created = await api(CONV_API.conversations, {
          method: 'POST',
          routeBound: true,
          body: payload
        });
        dialog.close();
        announce('Conversation created.');
        state.createKey = null;
        await refreshConversations();
        checkCurrent(ctx);
        var detail = normalizeDetail(created);
        var id = detail ? detail.id : (created && created.id);
        if (id) {
          globalThis.history.pushState(null, '', '/conversations.html?conversation=' + encodeURIComponent(id));
          await openConversation(id, { initial: true });
        }
      } catch (error) {
        if (!current(ctx) || error && (error.kind === 'auth' || error.kind === 'stale')) return;
        showFormError(error.message || 'Creating the conversation failed.');
      } finally {
        createButton.disabled = false;
      }
    });
    for (var radio of form.elements['conv-kind']) {
      radio.addEventListener('change', function () {
        byId('group-title-field').hidden = form.elements['conv-kind'].value !== 'group';
      });
    }
    filter.addEventListener('input', function () { renderParticipantChoices(filter.value); });

    function showFormError(message) {
      errorBox.textContent = message;
      errorBox.hidden = false;
    }
    function renderParticipantChoices(query) {
      var needle = String(query || '').trim().toLowerCase();
      list.replaceChildren();
      var participants = state.participants;
      var shown = participants.filter(function (participant) {
        return !needle || participant.displayName.toLowerCase().includes(needle) || participant.actor.toLowerCase().includes(needle);
      });
      if (!shown.length) {
        list.append(el('p', 'conv-identity conv-identity--none', state.participantsLoaded ? 'No registered participants match.' : 'Loading registered participants…'));
        return;
      }
      for (let participant of shown) {
        let check = document.createElement('input');
        check.type = 'checkbox';
        check.value = participant.actor;
        check.checked = !!participant.chosen;
        check.addEventListener('change', function () {
          var active = state.participants.find(function (p) { return p.actor === participant.actor; });
          if (active) active.chosen = check.checked;
        });
        var row = el('label', 'conv-identity');
        row.append(check, el('span', null, participant.displayName + ' — ' + participant.actor));
        row.append(el('span', 'dk-badge conv-identity__kind', participant.kind === 'agent' ? 'Agent' : 'Human'));
        list.append(row);
      }
    }
  }

  /* ---- routing: ?conversation=<id> URL state ---- */

  function currentRouteId() {
    return new URLSearchParams(globalThis.location.search).get('conversation');
  }
  function route() {
    var id = currentRouteId();
    if (id) { openConversation(id, { initial: !(state.threads[id] && state.threads[id].loaded) }); return; }
    if (!globalThis.matchMedia(PHONE).matches && state.conversations.length) {
      var first = state.conversations[0];
      globalThis.history.replaceState(null, '', '/conversations.html?conversation=' + encodeURIComponent(first.id));
      openConversation(first.id, { initial: true });
      return;
    }
    changeRoute(null);
    if (globalThis.dakitSplit) globalThis.dakitSplit.show(byId('conv-split'), 'list', { focus: false });
    render();
  }

  /* ---- wiring and bootstrap ---- */

  function wireListClicks() {
    byId('conv-list').addEventListener('click', function (event) {
      var row = event.target.closest('.dk-split__item');
      if (!row) return;
      event.preventDefault();
      var href = row.getAttribute('href');
      var id = new URLSearchParams(href.split('?')[1] || '').get('conversation');
      if (id !== state.currentId) {
        globalThis.history.pushState(null, '', href);
        openConversation(id, { initial: true });
      }
      if (globalThis.dakitSplit) globalThis.dakitSplit.show(byId('conv-split'), 'detail');
    });
    byId('conv-detail').addEventListener('click', function (event) {
      var back = event.target.closest('.dk-split__back');
      if (!back) return;
      event.preventDefault();
      globalThis.history.pushState(null, '', back.getAttribute('href'));
      route();
    });
    globalThis.addEventListener('popstate', route);
  }

  function wireReconnectAndFocus() {
    async function resync() {
      await refreshConversations();
      if (state.currentId && state.selfActor) refreshThread(state.currentId, { stickToBottom: true });
      render();
    }
    globalThis.addEventListener('online', function () {
      state.offline = false;
      setListStatus('Back online. Syncing…');
      resync();
      render();
    });
    globalThis.addEventListener('offline', function () {
      state.offline = true;
      announce('You are offline. Messages wait as drafts until you reconnect.');
      render();
    });
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible') {
        resync();
      }
    });
    globalThis.addEventListener('focus', resync);
  }

  function wireSignOut() {
    byId('sign-out').addEventListener('click', async function () {
      var button = byId('sign-out');
      button.disabled = true;
      var logoutCsrf = csrf;
      forgetSession();
      try {
        var response = await fetch(CONV_API.logout, {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'X-CSRF-Token': logoutCsrf }
        });
        var target = (response.ok || response.status === 302) && response.headers.get('Location');
        globalThis.location.assign(target || CONV_API.login);
      } catch (e) { globalThis.location.assign(CONV_API.login); }
    });
  }

  async function bootstrap() {
    wireListClicks();
    wireReconnectAndFocus();
    wireNewConversation();
    wireSignOut();
    render();
    var session = await refreshSession();
    if (session !== 'ok') return;
    await loadParticipants();
    await refreshConversations();
    route();
  }

  var booted = false;
  function start() {
    if (booted) return;
    booted = true;
    bootstrap();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
