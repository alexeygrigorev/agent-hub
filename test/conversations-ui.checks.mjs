/* Static checks for the conversations UI. Runs standalone with plain node
   (node test/conversations-ui.checks.mjs) and is also imported by the vitest
   suite. Zero dependencies beyond node builtins plus the vendored dakit
   checkers. Fails with a nonzero exit / thrown error on any violation. */
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { checkBorders } from '../public/vendor/dakit/check-borders.mjs';
import { checkColors } from '../public/vendor/dakit/check-colors.mjs';
import { checkScale } from '../public/vendor/dakit/check-scale.mjs';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const pub = (name) => join(root, 'public', name);

function vendorLock() {
  const lock = JSON.parse(readFileSync(pub('vendor/dakit/dakit.lock.json'), 'utf8'));
  const problems = [];
  for (const [name, expected] of Object.entries(lock.files)) {
    const file = join(root, 'public/vendor/dakit', name);
    let actual;
    try {
      actual = createHash('sha256').update(readFileSync(file)).digest('hex');
    } catch (e) {
      problems.push(`missing vendored file: ${name}`);
      continue;
    }
    if (actual !== expected) problems.push(`vendored file changed against lock: ${name}`);
  }
  return problems;
}

function noHtmlInjection() {
  const source = readFileSync(pub('conversations.js'), 'utf8');
  const banned = ['innerHTML', 'insertAdjacentHTML', 'outerHTML', 'document.write', 'eval(', 'new Function'];
  return banned
    .filter((needle) => source.includes(needle))
    .map((needle) => `conversations.js uses banned DOM/eval API: ${needle}`);
}

function htmlHygiene() {
  const source = readFileSync(pub('conversations.html'), 'utf8');
  const problems = [];
  if (/<style[\s>]/i.test(source)) problems.push('conversations.html has an inline <style> block (CSP style-src forbids it)');
  if (/\sstyle\s*=\s*"/i.test(source)) problems.push('conversations.html has a style= attribute (CSP style-src forbids it)');
  const handlers = source.match(/\son[a-z]+\s*=\s*["']/gi) || [];
  if (handlers.length) problems.push(`conversations.html has inline event handler(s): ${handlers.join(' ')}`);
  for (const match of source.matchAll(/<script[^>]*\bsrc="([^"]+)"/gi)) {
    if (!match[1].startsWith('/')) problems.push(`script src is not same-origin root-relative: ${match[1]}`);
  }
  if (!/<script src="\/conversations\.js"><\/script>/.test(source)) {
    problems.push('conversations.html must load /conversations.js as a blocking head script for the theme boot');
  }
  if (!source.includes('<link rel="stylesheet" href="/vendor/dakit/dakit.css">')) {
    problems.push('conversations.html must ship the vendored dakit bundle');
  }
  if (!source.includes('class="dk-account-trigger__chev"') || source.includes('dk-account-trigger__chevron')) {
    problems.push('account trigger must use the official dk-account-trigger__chev class');
  }
  return problems;
}

function tokenOnlyCustomProperties() {
  const css = readFileSync(pub('conversations.css'), 'utf8');
  const dakitTokens = new Set(
    (readFileSync(pub('vendor/dakit/tokens.css'), 'utf8').match(/--dk-[a-z0-9-]+/g) || [])
  );
  const local = new Set(css.match(/--conv-[a-z0-9-]+/g) || []);
  const used = new Set(css.match(/var\((--[a-z0-9-]+)/g).map((m) => m.slice(4)));
  const problems = [];
  for (const token of used) {
    if (token.startsWith('--dk-') && !dakitTokens.has(token)) problems.push(`unknown dakit token in conversations.css: ${token}`);
    if (token.startsWith('--conv-') && !local.has(token)) problems.push(`undefined local token in conversations.css: ${token}`);
    if (!token.startsWith('--dk-') && !token.startsWith('--conv-')) problems.push(`non-dakit custom property in conversations.css: ${token}`);
  }
  return problems;
}

/* Protocol guard: the page may only talk to the M1 conversations contract.
   Banned = shapes of the pre-contract draft that guessed the API; required =
   exact endpoints/keys the M1 contract defines (M1-UI-REPAIR.md). */
function contractTransport() {
  const source = readFileSync(pub('conversations.js'), 'utf8');
  const problems = [];
  const banned = [
    ['/api/identities', 'participants live at GET /api/conversations/participants'],
    ['member_ids', 'create takes members: [<canonical actor>]'],
    ['last_read_message_id', 'read cursor posts { seq } or { message_id }'],
    ['sender_id', 'messages carry sender_actor (response), never a request or guessed key'],
    ['sent_at', 'messages carry created_at'],
  ];
  for (const [needle, why] of banned) {
    if (source.includes(needle)) problems.push(`conversations.js uses banned contract key "${needle}" (${why})`);
  }
  if (/has_more(?![\w])/.test(source)) problems.push('conversations.js uses bare has_more (contract: has_more_before / has_more_after)');
  if (/[?&]before=/.test(source)) problems.push('conversations.js uses a message-id before cursor (contract: before_seq)');
  const required = [
    ['/api/conversations/participants', 'verified self + participant identities'],
    ['X-CSRF-Token', 'session-authenticated mutations carry the CSRF token'],
    ['before_seq', 'older-history pagination'],
    ['after_seq', 'incremental polling'],
    ['?conversation=', 'URL state uses the conversation param'],
    ['seq: last.seq', 'read cursor posts the rendered seq'],
    ['idempotency_key', 'send and create are idempotent'],
    ['self.actor', 'self identity is the verified participants self.actor'],
  ];
  for (const [needle, why] of required) {
    if (!source.includes(needle)) problems.push(`conversations.js is missing contract key "${needle}" (${why})`);
  }
  return problems;
}

export function runChecks() {
  const problems = [
    ...vendorLock(),
    ...noHtmlInjection(),
    ...htmlHygiene(),
    ...tokenOnlyCustomProperties(),
    ...contractTransport()
  ];
  const targets = [pub('conversations.css'), pub('conversations.html')];
  const borderViolations = checkBorders(targets);
  const colorViolations = checkColors(targets);
  const scaleResult = checkScale(targets);
  for (const violation of borderViolations) problems.push(`border: ${violation}`);
  for (const violation of colorViolations) problems.push(`color: ${violation}`);
  for (const failure of scaleResult.failures || []) problems.push(`scale: ${failure}`);
  return problems;
}

const isMain = process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1];
if (isMain) {
  const problems = runChecks();
  if (problems.length) {
    for (const problem of problems) console.error('FAIL:', problem);
    process.exit(1);
  }
  console.log('conversations-ui static checks: PASS (vendor lock, borders, colors, scale, DOM-safety, CSP hygiene, M1 contract transport)');
}
