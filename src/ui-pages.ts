/** Presentation only. Device decisions still use Hub's existing session/CSRF endpoints. */
export const escapeHtml = (value: unknown): string => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]!));
export const UI_HEADERS = {
  'Cache-Control': 'private, no-store',
  'Content-Type': 'text/html; charset=utf-8',
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
};
export function authPage(title: string, lede: string, body = '', email = '', foot = '', returnTo = ''): string {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>${escapeHtml(title)} · Agent Hub</title><link rel="stylesheet" href="/vendor/dakit/dakit.css"><link rel="stylesheet" href="/auth.css"><script src="/auth.js"></script></head>
<body class="dk-auth-page"><header class="dk-auth-page__bar"><a class="dk-auth-page__brand" href="/">Agent Hub</a><div class="dk-auth-page__identity" role="status">${escapeHtml(email)}${email ? ` <a href="/auth/login${returnTo ? '?return_to='+encodeURIComponent(returnTo) : ''}">Switch account</a>` : ''}</div></header><main class="dk-auth-page__main"><section class="dk-auth-card" aria-labelledby="auth-title"><header class="dk-auth-card__head"><h1 class="dk-auth-card__title" id="auth-title" tabindex="-1">${escapeHtml(title)}</h1><p class="dk-auth-card__lede">${escapeHtml(lede)}</p></header>${body}<footer class="dk-auth-card__foot">${foot}<nav class="hub-auth-nav" aria-label="Agent Hub"><a href="/conversations.html">Conversations</a><a href="/fleet.html">Fleet</a><a href="/metrics.html">Metrics</a><a href="/device">Pair a device</a></nav></footer></section></main><script src="/vendor/dakit/shell.js"></script></body></html>`;
}
export function tokenPage(message: string, next: string | null, status: number): string {
  return authPage('Sign in to Agent Hub', 'Sign in with the admin token kept on your machine.', `${status >= 400 ? `<p class="dk-callout dk-callout--${status === 429 ? 'warning' : 'danger'}" role="alert">${escapeHtml(message)}</p>` : ''}<form method="post" action="/auth/token" class="dk-stack"><input type="hidden" name="return_to" value="${escapeHtml(next || '')}"><div><label class="dk-label" for="admin-token">Admin token</label><input class="dk-input" id="admin-token" name="token" type="password" autocomplete="current-password" required></div><div class="dk-auth-card__actions"><button class="dk-button dk-button--primary dk-button--lg" type="submit">Sign in</button></div></form>`, '', 'Keep your token private.');
}
export function authFailure(request: Request, error: string, status: number, extra: Record<string,string> = {}, category?: string): Response {
  const cat = category || error;
  const respHeaders = { ...UI_HEADERS, ...extra, 'X-Auth-Error': cat };
  if (!request.headers.get('Accept')?.includes('text/html')) {
    return Response.json({ error, category: cat }, { status, headers: respHeaders });
  }
  const forbidden = status === 403, unavailable = status === 503;
  let lede = 'This sign-in link is invalid or expired. Start sign-in again.';
  if (unavailable) {
    lede = 'Sign-in is not configured. Contact your hub administrator.';
  } else if (forbidden) {
    lede = 'This account cannot administer this hub. Sign in with an approved account.';
  } else if (cat === 'state_already_consumed' || cat === 'state_replayed') {
    lede = 'This sign-in link has already been used. Start sign-in again.';
  } else if (cat === 'state_expired') {
    lede = 'This sign-in link has expired. Start sign-in again.';
  } else if (cat === 'missing_cookie') {
    lede = 'Sign-in session cookie was not found or expired. Ensure cookies are enabled and try again.';
  } else if (cat.startsWith('oauth_error:') || cat.startsWith('oauth_')) {
    lede = `Provider returned an error: ${cat.replace(/^oauth_error:|^oauth_/, '')}. Start sign-in again.`;
  }
  const diagBody = `<div class="dk-auth-card__actions"><a class="dk-button dk-button--primary dk-button--lg" href="/auth/login">Try sign-in again</a></div><p class="dk-hint" id="auth-diagnostic" data-category="${escapeHtml(cat)}" style="margin-top:1.25rem;font-size:0.8125rem;color:var(--dk-text-muted,#888);">Diagnostic reference: <code>${escapeHtml(cat)}</code></p>`;
  return new Response(authPage(unavailable ? 'Sign-in unavailable' : forbidden ? 'Access not allowed' : 'Sign-in could not finish', lede, diagBody), { status, headers: respHeaders });
}

export function deviceEntry(email: string): string {
  return authPage('Pair a device', 'Enter the code shown in your terminal, then compare the request before approving.', `<form method="get" action="/device" class="dk-stack"><div><label class="dk-label" for="device-code">Device code</label><input class="dk-input dk-code-input" id="device-code" name="code" required autocomplete="off" spellcheck="false" placeholder="XXXX-XXXX" aria-describedby="code-hint"><p class="dk-hint" id="code-hint">Use the complete code from the device you want to authorize.</p></div><div class="dk-auth-card__actions"><button class="dk-button dk-button--primary dk-button--lg">Continue</button></div></form>`, email);
}
export function deviceResult(state: string, email: string): string {
  const outcomes: Record<string,[string,string]> = {
    approved:['Device authorized','Return to your terminal to finish signing in.'],
    consumed:['Device authorized','This code has already been used. Return to your terminal.'],
    denied:['Request denied','Nothing was authorized. Run hub login again if you meant to sign in.'],
    expired:['Code expired','Run hub login again for a new code.'],
    unknown:['Code not found','This code is wrong, expired, or already used. Run hub login again for a new one.'],
  };
  const [title,lede] = outcomes[state] || outcomes.unknown;
  return authPage(title,lede, state === 'expired' || state === 'unknown' ? '<div class="dk-auth-card__actions"><a class="dk-button dk-button--secondary dk-button--lg" href="/device">Enter another code</a></div>' : '<p role="status">You can close this page.</p>',email);
}
export function deviceReviewHuman(pairing: any, email: string, now: number): string {
  const e = escapeHtml;
  const facts = [['Code', pairing.user_code], ['Kind', 'human'], ['Label', pairing.name], ['Started', pairing.created_at]];
  return authPage('Approve human login?', 'Only approve a login you started. This grants full admin access for 30 days.', `<div id="device-confirm" class="dk-stack"><div><span class="dk-label">Device code</span><div class="dk-mono">${e(pairing.user_code)}</div><p class="dk-hint">Check that this matches your terminal exactly. <a href="/device?code=${e(pairing.user_code)}">Reload this request</a> if anything looks wrong.</p></div></div><div id="device-review" data-code="${e(pairing.user_code)}" data-actor="" data-kind="human" data-machine="" data-remaining="${Math.max(0, Date.parse(pairing.expires_at) - now)}" class="dk-auth-card__body"><dl class="dk-dl">${facts.map(([k, v]) => `<dt>${k}</dt><dd class="dk-mono">${e(v)}</dd>`).join('')}<dt>Expires in</dt><dd class="dk-mono" id="device-countdown" aria-live="off"></dd></dl>
  <form id="approveForm" method="post" action="/api/device/approve/${e(pairing.user_code)}" class="dk-stack">
  <fieldset class="hub-permissions"><legend class="dk-label">Granted permissions</legend><p class="dk-hint">Human logins always receive full admin access. No machine, project or tag is granted here.</p><label class="dk-check"><input type="checkbox" name="scope" value="admin" checked><span class="dk-mono">admin — full access (30 days)</span></label></fieldset>
  <p class="dk-callout dk-callout--warning">This authorizes a human operator CLI as yourself for up to 30 days, or until revoked or removed from the admin allowlist.</p>
  <p id="device-error" class="dk-callout" role="alert" hidden></p><div class="dk-auth-card__actions"><button class="dk-button dk-button--secondary dk-button--lg" type="button" id="device-deny">Deny</button><button class="dk-button dk-button--primary dk-button--lg" type="submit" id="device-approve">Approve</button></div><a id="device-reload" href="/device?code=${e(pairing.user_code)}" hidden>Reload the request</a></form></div><div id="device-live" class="dk-visually-hidden" aria-live="polite"></div>`, email, "Didn't start this? Deny it. Nothing is shared.", "/device?code=" + pairing.user_code);
}
export function deviceReview(pairing: any, email: string, address: {project:string|null;tag:string}|null, registeredMachine: string|null, machines: any[], now: number, displayedActor: string|null): string {
  const e = escapeHtml, scopes: string[] = JSON.parse(pairing.requested_scopes);
  const options = machines.map(m => `<option value="${e(m.id)}"${m.id === registeredMachine ? ' selected' : ''}>${e(m.name || m.id)} (${e(m.id)})</option>`).join('');
  const facts = [['Code',pairing.user_code],['Kind',pairing.client_kind],['Name',pairing.name],['Hostname',pairing.hostname],['Country',pairing.ip_country || 'Unknown'],['Actor',displayedActor || 'New host'],['Started',pairing.created_at]];
  return authPage('Pair a device', 'Compare the code with the terminal on the device you want to authorize.', `<form id="device-confirm" class="dk-stack"><div><label class="dk-label" for="confirm-code">Device code</label><input class="dk-input dk-code-input" id="confirm-code" name="code" value="${e(pairing.user_code)}" required autocomplete="off" aria-describedby="confirm-hint"><p class="dk-hint" id="confirm-hint">Check that this matches your terminal exactly.</p></div><div class="dk-auth-card__actions"><button class="dk-button dk-button--primary dk-button--lg">Continue</button></div></form><div id="device-review" hidden data-code="${e(pairing.user_code)}" data-actor="${e(displayedActor || '')}" data-kind="${e(pairing.client_kind)}" data-machine="${e(registeredMachine || '')}" data-remaining="${Math.max(0,Date.parse(pairing.expires_at)-now)}" class="dk-auth-card__body"><dl class="dk-dl">${facts.map(([k,v])=>`<dt>${k}</dt><dd class="dk-mono">${e(v)}${k === 'Hostname' ? '<span class="dk-dl__note">Reported by the device</span>' : ''}</dd>`).join('')}<dt>Expires in</dt><dd class="dk-mono" id="device-countdown" aria-live="off"></dd></dl>
  <form id="approveForm" method="post" action="/api/device/approve/${e(pairing.user_code)}" class="dk-stack">
  <fieldset class="hub-permissions"><legend class="dk-label">Granted scopes</legend><p class="dk-hint">Requested permissions are selected. Clear any permission you do not want to grant.</p>${scopes.map(s=>`<label class="dk-check"><input type="checkbox" name="scope" value="${e(s)}" checked><span class="dk-mono">${e(s)}</span></label>`).join('')}</fieldset>
  ${pairing.client_kind === 'agent' ? `<div aria-label="Agent machine approval"><p>Project: ${e(address?.project || '(projectless)')} / Tag: ${e(address?.tag || '')}</p><p>Current registered machine: ${e(registeredMachine || 'none')}</p><label class="dk-label" for="agentMachine">Approved machine</label><select class="dk-select" id="agentMachine" name="machine_id"><option value="">${registeredMachine ? 'Preserve registered assignment' : 'Machine-less (native bridge unavailable)'}</option>${options}</select><p class="dk-hint">Choose an enrolled machine only after verifying this agent belongs there. This grants no host privileges. Existing assignments cannot be transferred here.</p></div>` : `<div><label class="dk-label" for="host-machine">Machine ID (optional)</label><input class="dk-input dk-mono" id="host-machine" name="machine_id"><p class="dk-hint">Leave empty to register a new host, or enter its existing machine ID.</p></div>`}
  <p class="dk-callout dk-callout--warning">${pairing.client_kind === 'host' ? 'This host can report machine activity and receive host work within the selected permissions.' : 'This agent can act as the identity shown above within the selected permissions.'} Access lasts up to 30 days, or until revoked.</p>
  <p id="device-error" class="dk-callout" role="alert" hidden></p><div class="dk-auth-card__actions"><button class="dk-button dk-button--secondary dk-button--lg" type="button" id="device-deny">Deny</button><button class="dk-button dk-button--primary dk-button--lg" type="submit" id="device-approve">Approve</button></div><a id="device-reload" href="/device?code=${e(pairing.user_code)}" hidden>Reload the request</a></form></div><div id="device-live" class="dk-visually-hidden" aria-live="polite"></div>`, email, "Didn't start this? Deny it. Nothing is shared.", "/device?code="+pairing.user_code);
}
