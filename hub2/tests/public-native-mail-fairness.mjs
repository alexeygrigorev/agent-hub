// Private candidate; default is INERT. Actual scenario requires reviewed adapter/backend.
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { openBackend, need, readPlain } from './public-native-mail-fixture.mjs';

const SAFE_REASONS = new Set(['composition_deadline','operation_deadline','expected_event_absent','source_changed','adapter_changed','adapter_factory_changed','literal_owned_origin_required','scratch_budget_exceeded','scratch_entry_limit','capture_byte_limit','actual_cli_failed','receiver_ended_before_event','actual_child_exit73_required','first_receipt_forbidden','actual_public_later_receipt_required','no_reexecution_clear_or_fabricated_receipt','child_failed','external_abort']);
export function safeFailure(error) { return SAFE_REASONS.has(error?.message) ? error.message : 'composition_unqualified'; }

export async function runPublicNativeMail(input, signal) {
  let io = null, first = null, later = null, result = null, failure = null, phase = 'prepare';
  const workers = [];
  try {
    io = await openBackend(input, {signal});
    const left = cap => io.budget.remaining(cap); // ONE budget, no setup renewal.
    phase = 'public_seed';
    const seed = await io.seedPublicBinding(left());
    need(seed.session.state === 'running' && typeof seed.session.native_id === 'string'
      && Number.isSafeInteger(seed.session.epoch) && seed.session.epoch > 0
      && seed.offers.length === 0 && seed.session.attention === null, 'synthetic_binding_required');
    phase = 'first_receiver';
    const crash = await io.openReviewedPublicReceiver({mode: 'crash'}); workers.push(crash);
    await crash.waitConnected(left(5000));
    phase = 'first_cli_and_effect';
    first = await io.sendActualCliMessage('uncertain-first', seed.recipient, left(5000));
    need(await crash.waitExit(left(5000)) === 73, 'actual_child_exit73_required');
    let disk = await io.readOwnJournalReadonly({firstId: first.id, firstEpoch: first.epoch});
    need(disk.effects.length === 1 && disk.effects[0].id === first.id && disk.effects[0].receipt === null
      && disk.first.id === first.id && disk.first.state === 'executing'
      && disk.first.receipt === null && disk.first.receiptOutbox === false, 'actual_durable_uncertainty_required');
    need((await io.publicMessageStatus(first.id)).delivered_at === null, 'first_receipt_forbidden');
    phase = 'real_reconnect_uncertainty';
    const receive = await io.openReviewedPublicReceiver({mode: 'receive'}); workers.push(receive);
    await receive.waitConnected(left(5000));
    await receive.waitActualReceived(first.id, left(5000));
    await receive.waitActualSentError(first.id, 'native_mail_outcome_uncertain', left(5000));
    await io.waitPublicSessionAttention(seed.session.id, 'native_mail_outcome_uncertain');
    need((await io.publicMessageStatus(first.id)).delivered_at === null, 'uncertainty_cannot_heal');
    phase = 'later_cli';
    later = await io.sendActualCliMessage('later-after-real-uncertainty', seed.recipient, left(5000));
    need(later.id !== first.id, 'distinct_later_required');
    // No later-frame injection exists. Oldest pending LIMIT1 may FAIL this wait.
    phase = 'later_server_delivery';
    await receive.waitActualReceived(later.id, left(5000));
    phase = 'later_actual_receipt';
    const posted = await receive.waitActualReceiptPosted(later.id, left(5000));
    const publicLater = await io.publicMessageStatus(later.id);
    need(publicLater.delivered_at != null && posted.native_receipt.provider === 'aplexer' && publicLater.native_receipt?.provider === 'aplexer' && publicLater.native_receipt?.message_id === posted.native_receipt.message_id
      && publicLater.native_receipt?.session_id === seed.session.id
      && publicLater.native_receipt?.recipient === seed.recipient, 'actual_public_later_receipt_required');
    disk = await io.readOwnJournalReadonly({firstId: first.id, firstEpoch: first.epoch, laterId: later.id, laterEpoch: later.epoch});
    need(disk.first.state === 'executing' && disk.first.receipt === null && disk.first.receiptOutbox === false
      && disk.later.state === 'receipted' && disk.effects.length === 2
      && disk.effects.filter(v => v.id === first.id).length === 1
      && disk.effects.filter(v => v.id === later.id && v.receipt === posted.native_receipt.message_id).length === 1
      && disk.later.receipt.native_receipt.message_id === posted.native_receipt.message_id
      && (await io.publicMessageStatus(first.id)).delivered_at === null
      && receive.unexpectedDispatches === 0, 'no_reexecution_clear_or_fabricated_receipt');
    left(); result = {status: 'observed_public_progression', first: first.id, later: later.id,
      native_effect_fake: true, full63_retirement: false, complete: false};
  } catch (error) { failure = error; result = {status: 'FAILED_OR_UNKNOWN', reason: safeFailure(error), phase, first: first?.id, later: later?.id, complete: false}; }
  finally {
    const cleanup = [];
    for (const worker of workers.reverse()) { try { await io.closeOwnedWorker(worker); } catch { cleanup.push('owned_worker_cleanup_unknown'); } }
    try { if (io) await io.retainAndDisposeOwnFixture({...result, cleanup}); }
    catch { cleanup.push('owned_fixture_cleanup_unknown'); }
    if (cleanup.length) { result = {...result, status: 'FAILED_OR_UNKNOWN', cleanup}; failure ??= new Error('cleanup_unknown'); }
  }
  // A diagnostic failure remains failure; never resample/repair/ACK first.
  return {result: {...result, evidence_directory: io?.evidenceDirectory}, exitCode: failure ? 1 : 0};
}

async function stdinConfig() {
  let total = 0; const chunks = []; const timer = setTimeout(() => process.stdin.destroy(new Error('config_deadline')), 2000);
  try { for await (const chunk of process.stdin) { total += chunk.length; need(total <= 8192, 'config_limit'); chunks.push(chunk); }
    return JSON.parse(Buffer.concat(chunks).toString('utf8')); } finally { clearTimeout(timer); }
}
async function main() {
  if (process.argv.length === 2) { console.log(JSON.stringify({status: 'INERT', execution_authorized: false, complete: false})); return; }
  need(process.argv.length === 5 && process.argv[2] === '--execute-reviewed' && process.argv[3] === '--backend-sha256', 'explicit_reviewed_invocation_required');
  const backend = new URL('./public-native-mail-fixture.mjs', import.meta.url);
  need(createHash('sha256').update(readPlain(backend, 131072)).digest('hex') === process.argv[4], 'backend_changed');
  const controller = new AbortController(); const abort = () => controller.abort();
  process.once('SIGINT', abort); process.once('SIGTERM', abort);
  try { const {result, exitCode} = await runPublicNativeMail(await stdinConfig(), controller.signal); console.log(JSON.stringify(result)); process.exitCode = exitCode; }
  finally { process.removeListener('SIGINT', abort); process.removeListener('SIGTERM', abort); }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(() => { console.error('public_native_mail_backend_refused'); process.exitCode = 2; });
}
