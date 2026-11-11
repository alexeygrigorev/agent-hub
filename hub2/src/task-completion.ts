// Hub-owned task completion: a native process exit is not task completion.
// Started sessions receive a completion contract; Hub decides the intake
// outcome from a correlated completion claim, continues unfinished work a
// bounded number of times and delivers the terminal outcome to the requester.

export const COMPLETION_ACTOR = 'hub2-completion';
export const CONTRACT_MARKER = '\n\n--- Hub task completion contract ---\n';
export const CONTINUATION_MARKER = '--- Hub continuation ---\n';
export const CONTINUATION_END = '\n--- Original task ---\n';
/** Consecutive Hub-started continuations after one requester start. */
export const MAX_AUTO_CONTINUATIONS = 2;
export const MAX_CONTINUATION_TRIES = 3;
export const MAX_DELIVERY_TRIES = 5;
/** Pending completion actions are found only among recently updated intakes. */
export const COMPLETION_ACTION_WINDOW_MS = 24 * 60 * 60_000;
const PROMPT_LIMIT = 8192;
const LAUNCH_LIMIT = 11_500;

export type CompletionClaim = { summary: string; met: number };
export type CompletionAction = {
  state: 'pending' | 'started' | 'superseded' | 'refused' | 'delivered' | 'failed' | 'skipped';
  key: string; tries: number; next_action_at: number | null; last_reason?: string | null;
  session_id?: string; message_id?: string; recipient?: string;
};
export type CompletionRecord = {
  controlled: true; session_id: string; outcome: 'claimed' | 'unfinished'; reason: string;
  depth: number; summary?: string; continuation?: CompletionAction; delivery?: CompletionAction;
};

const bytes = (value: string) => new TextEncoder().encode(value).byteLength;

function clip(value: string, limit: number): string {
  if (bytes(value) <= limit) return value;
  let result = value.slice(0, limit);
  while (result && bytes(result + '…') > limit) result = result.slice(0, -1);
  return result + '…';
}

/** The contract appended to a controlled session's prompt, sized so the whole
 * launch spec stays under the Machine's 12 KB launch limit. */
export function completionContract(intakeId: string, sessionId: string, criteria: string[], budget: number): string {
  const head = `Hub intake ${intakeId}, Hub session ${sessionId}. Process exit is not task completion: Hub marks this task finished only from your completion record, and a session that exits without one is treated as unfinished.\n`;
  const tail = `When every criterion is met with proof, before you exit write the completion record:\n`
    + `  hub intake append ${intakeId} --key complete-${sessionId} --version 1 --event-file <file>\n`
    + `with <file> containing {"kind":"task_complete","session_id":"${sessionId}","summary":"<what was delivered>","acceptance":[{"status":"met","proof":"<commit, PR, CI, deploy or live evidence>"}]} and exactly one acceptance entry per criterion, in order.\n`
    + `If you cannot finish, do not write that record: append a note with the current state and the next step (hub intake append ${intakeId} --note "...") and exit. Hub continues unfinished tasks itself (bounded) and delivers the outcome to the requester.`;
  const header = `Acceptance criteria (${criteria.length}; full text: hub intake show ${intakeId}):\n`;
  const fixed = bytes(CONTRACT_MARKER + head + header + tail);
  let room = budget - fixed;
  if (room < 0) return CONTRACT_MARKER + head + tail;
  const lines: string[] = [];
  for (const [index, criterion] of criteria.entries()) {
    const prefix = `${index + 1}. `, left = criteria.length - index;
    const share = Math.max(0, Math.floor(room / left) - bytes(prefix) - 1);
    const line = prefix + clip(criterion.replace(/\s+/g, ' ').trim(), share) + '\n';
    if (share < 16 || bytes(line) > room) break;
    lines.push(line); room -= bytes(line);
  }
  return CONTRACT_MARKER + head + header + lines.join('') + tail;
}

/** Attach the contract to the prompt within the launch budget. */
export function promptWithContract(prompt: string, launchWithoutPrompt: unknown, intakeId: string, sessionId: string, criteria: string[]): string {
  const base = bytes(JSON.stringify({ ...(launchWithoutPrompt as object), prompt }));
  // JSON escaping can grow multi-byte or control characters; keep a margin.
  const budget = Math.min(6000, Math.floor((LAUNCH_LIMIT - base) / 1.2));
  return prompt + completionContract(intakeId, sessionId, criteria, budget);
}

/** The task prompt without Hub-added contract or continuation preamble. */
export function basePrompt(prompt: string): string {
  let value = prompt;
  const contract = value.indexOf(CONTRACT_MARKER);
  if (contract >= 0) value = value.slice(0, contract);
  if (value.startsWith(CONTINUATION_MARKER)) {
    const end = value.indexOf(CONTINUATION_END);
    if (end >= 0) value = value.slice(end + CONTINUATION_END.length);
  }
  return value;
}

export function continuationPrompt(intakeId: string, depth: number, previousSession: string, reason: string, original: string): string {
  const preamble = CONTINUATION_MARKER
    + `Hub started this session automatically (continuation ${depth} of at most ${MAX_AUTO_CONTINUATIONS}) because session ${previousSession} ended without completing intake ${intakeId} (${reason}). `
    + `First read the intake history (hub intake history ${intakeId}) and the current workspace state, then continue the remaining work; do not redo finished steps.`
    + CONTINUATION_END;
  return preamble + clip(original, PROMPT_LIMIT - bytes(preamble));
}

/** A completion claim counts only for this exact session and covers every criterion. */
export function completionClaim(evidence: unknown, sessionId: string, criteriaCount: number): CompletionClaim | undefined {
  if (!evidence || typeof evidence !== 'object' || Array.isArray(evidence)) return;
  const record = evidence as Record<string, unknown>;
  if (record.kind !== 'task_complete' || record.session_id !== sessionId || !Array.isArray(record.acceptance)) return;
  if (criteriaCount < 1 || record.acceptance.length !== criteriaCount) return;
  for (const item of record.acceptance) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return;
    const entry = item as Record<string, unknown>;
    if (entry.status !== 'met' || typeof entry.proof !== 'string' || !entry.proof.trim()) return;
  }
  return { summary: typeof record.summary === 'string' ? clip(record.summary.trim(), 1000) : '', met: criteriaCount };
}

export function retryDelay(tries: number): number {
  return Math.min(30 * 60_000, 60_000 * 4 ** Math.max(0, tries - 1));
}

export function deliveryBody(input: {
  intake_id: string; title: string; outcome: 'claimed' | 'unfinished'; reason: string; session_id: string;
  summary?: string; continuation?: CompletionAction; depth: number;
}): string {
  const lines = input.outcome === 'claimed'
    ? [`Task finished, awaiting review: ${input.title}`,
      `Intake ${input.intake_id}; session ${input.session_id} recorded a completion claim covering every acceptance criterion.`,
      ...(input.summary ? ['Summary: ' + input.summary] : []),
      `Proof: hub intake history ${input.intake_id}. Accept with hub intake update ${input.intake_id} --status accepted, or reopen it.`]
    : [`Task NOT finished: ${input.title}`,
      `Intake ${input.intake_id}; last session ${input.session_id} ended without a completion claim (${input.reason}).`,
      input.continuation?.state === 'refused'
        ? `Hub could not start a continuation (${input.continuation.last_reason ?? 'refused'}).`
        : input.depth >= MAX_AUTO_CONTINUATIONS
          ? `Hub already continued it ${input.depth} times automatically; it needs a decision.`
          : 'Hub does not continue this exit automatically.',
      `State and notes: hub intake history ${input.intake_id}. Restart with hub sessions start --intake-task-id ${input.intake_id} when ready.`];
  return clip(lines.join('\n'), 12000);
}
