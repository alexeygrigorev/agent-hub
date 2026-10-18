import type { SqlStorage, CallerContext } from './bus';
import type { IntakeService } from './intake';
import { parseCanonicalHumanActor, type VerifiedConversationPrincipal } from './conversations';
import { credentialActor } from './agent_registry';

export type FactType =
  | 'owner_acceptance'
  | 'first_action'
  | 'due_obligation'
  | 'independent_review'
  | 'accepted_outcome'
  | 'human_delivery';

export type FactState =
  | 'known'
  | 'unknown'
  | 'refused'
  | 'open'
  | 'overdue'
  | 'answered'
  | 'proceeded'
  | 'answered-after-proceeding';

export interface FollowThroughFact {
  fact_type: FactType;
  state: FactState;
  source_identity: string | null;
  authority: string;
  observation: Record<string, unknown> | null;
  revision: number | null;
}

export interface ObligationItem {
  id: string;
  text_hash: string;
  text: string;
  asked_at: number;
  deadline: number;
  wait_s: number;
  default_action: string;
  status: 'open' | 'answered' | 'proceeded' | 'answered-after-proceeding';
  overdue: boolean;
  late_reconciled: boolean;
  answered_at?: number | null;
  proceeded_at?: number | null;
  source?: Record<string, unknown> | null;
}

export interface FollowThroughTargetView {
  available: boolean;
  canonical_status: string | null;
  terminal?: boolean;
  complete?: boolean;
  title?: string;
  project?: string;
}

export interface FollowThroughView {
  id?: string;
  link_id?: string;
  conversation_id: string;
  kind: string;
  ref: string;
  target?: FollowThroughTargetView;
  facts: {
    owner_acceptance: FollowThroughFact;
    first_action: FollowThroughFact;
    due_obligation: FollowThroughFact;
    independent_review: FollowThroughFact;
    accepted_outcome: FollowThroughFact;
    human_delivery: FollowThroughFact;
  };
  overdue_obligation: boolean;
  summary: {
    ready: boolean;
    complete: boolean;
    blocked: boolean;
  };
}

export function sha256Sync(input: string): string {
  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);
  const bytes = new TextEncoder().encode(input);
  const bitLen = bytes.length * 8;
  const padLen = (bytes.length + 9 + 63) & ~63;
  const padded = new Uint8Array(padLen);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  const view = new DataView(padded.buffer);
  view.setUint32(padLen - 8, Math.floor(bitLen / 0x100000000), false);
  view.setUint32(padLen - 4, bitLen >>> 0, false);

  let h0 = 0x6a09e667, h1 = 0xbb67ae85, h2 = 0x3c6ef372, h3 = 0xa54ff53a;
  let h4 = 0x510e527f, h5 = 0x9b05688c, h6 = 0x1f83d9ab, h7 = 0x5be0cd19;
  const w = new Uint32Array(64);

  for (let i = 0; i < padLen; i += 64) {
    for (let t = 0; t < 16; t++) {
      w[t] = view.getUint32(i + t * 4, false);
    }
    for (let t = 16; t < 64; t++) {
      const s0 =
        ((w[t - 15] >>> 7) | (w[t - 15] << 25)) ^
        ((w[t - 15] >>> 18) | (w[t - 15] << 14)) ^
        (w[t - 15] >>> 3);
      const s1 =
        ((w[t - 2] >>> 17) | (w[t - 2] << 15)) ^
        ((w[t - 2] >>> 19) | (w[t - 2] << 13)) ^
        (w[t - 2] >>> 10);
      w[t] = (w[t - 16] + s0 + w[t - 7] + s1) >>> 0;
    }
    let a = h0, b = h1, c = h2, d = h3, e = h4, f = h5, g = h6, h = h7;
    for (let t = 0; t < 64; t++) {
      const S1 =
        ((e >>> 6) | (e << 26)) ^
        ((e >>> 11) | (e << 21)) ^
        ((e >>> 25) | (e << 7));
      const ch = (e & f) ^ (~e & g);
      const temp1 = (h + S1 + ch + K[t] + w[t]) >>> 0;
      const S0 =
        ((a >>> 2) | (a << 30)) ^
        ((a >>> 13) | (a << 19)) ^
        ((a >>> 22) | (a << 10));
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (S0 + maj) >>> 0;
      h = g; g = f; f = e; e = (d + temp1) >>> 0;
      d = c; c = b; b = a; a = (temp1 + temp2) >>> 0;
    }
    h0 = (h0 + a) >>> 0; h1 = (h1 + b) >>> 0; h2 = (h2 + c) >>> 0; h3 = (h3 + d) >>> 0;
    h4 = (h4 + e) >>> 0; h5 = (h5 + f) >>> 0; h6 = (h6 + g) >>> 0; h7 = (h7 + h) >>> 0;
  }
  return [h0, h1, h2, h3, h4, h5, h6, h7]
    .map(val => val.toString(16).padStart(8, '0'))
    .join('');
}

export class ConversationFollowThroughService {
  constructor(
    public readonly sql: SqlStorage,
    private readonly now: () => number = Date.now,
    private readonly getIntakeService?: (sql: any) => any,
  ) {}

  private getIntake(): any | null {
    if (this.getIntakeService) return this.getIntakeService(this.sql);
    return null;
  }

  /**
   * Derives all 6 distinct typed authority facts for a given link target.
   * Strict negative acceptance matrix is enforced:
   * - Mere task owner text is not acceptance
   * - Launcher start is not first action
   * - Process exit is not outcome acceptance
   * - Routing job under label review is not independent review
   * - Read cursor alone is not task delivery
   */
  public deriveLinkFacts(
    link: { id?: string; conversation_id: string; kind: string; ref: string },
    targetRow: any | null,
    _principal?: VerifiedConversationPrincipal,
    targetView?: FollowThroughTargetView,
  ): FollowThroughView {
    const nowMs = this.now();
    const kind = link.kind;
    let task: any = null;
    let job: any = null;
    let launch: any = null;

    if (kind === 'intake_task') {
      task = targetRow;
    } else if (kind === 'routing_job' || kind === 'review' || kind === 'outcome') {
      job = targetRow;
      if (job?.launch_id) {
        launch = this.readRow('fleet_launches', job.launch_id);
      }
    } else if (kind === 'fleet_launch') {
      launch = targetRow;
    }

    const ownerAcceptance = this.deriveOwnerAcceptanceFact(task, job);
    const firstAction = this.deriveFirstActionFact(task, job, launch, link.conversation_id);
    const obligationResult = this.deriveDueObligationFact(task, nowMs);
    const independentReview = this.deriveIndependentReviewFact(task, job, kind);
    const acceptedOutcome = this.deriveAcceptedOutcomeFact(task, job, launch);
    const humanDelivery = this.deriveHumanDeliveryFact(task, link.conversation_id);

    const hasApprovedReview =
      independentReview.state === 'known' &&
      independentReview.observation?.verdict === 'approved';

    const hasAcceptedOutcome =
      acceptedOutcome.state === 'known' &&
      acceptedOutcome.observation?.verdict === 'accepted';

    // Cross-check: outcome and review must pin to the same source SHA and reference review
    let outcomePinsReview = false;
    if (hasApprovedReview && hasAcceptedOutcome) {
      const reviewSha = String(independentReview.observation?.source_sha || '').toLowerCase();
      const outcomeSha = String(acceptedOutcome.observation?.source_sha || '').toLowerCase();
      const reviewId = String(independentReview.observation?.review_id || '');
      const outcomeReviewRef = String(acceptedOutcome.observation?.review_ref || '');
      if (
        reviewSha &&
        outcomeSha &&
        reviewSha === outcomeSha &&
        Boolean(outcomeReviewRef && outcomeReviewRef === reviewId)
      ) {
        outcomePinsReview = true;
      }
    }

    const hasHumanDelivery = humanDelivery.state === 'known';

    const ready =
      ownerAcceptance.state === 'known' &&
      obligationResult.fact.state !== 'refused' &&
      !obligationResult.overdue;

    const complete =
      ownerAcceptance.state === 'known' &&
      firstAction.state === 'known' &&
      hasApprovedReview &&
      hasAcceptedOutcome &&
      outcomePinsReview &&
      hasHumanDelivery &&
      !obligationResult.overdue &&
      obligationResult.items.every(i => i.status !== 'open');

    const blocked =
      obligationResult.overdue ||
      ownerAcceptance.state === 'refused' ||
      independentReview.state === 'refused' ||
      independentReview.observation?.verdict === 'changes_requested' ||
      independentReview.observation?.verdict === 'rejected' ||
      acceptedOutcome.state === 'refused' ||
      acceptedOutcome.observation?.verdict === 'rejected';

    let target = targetView;
    if (!target) {
      if (targetRow) {
        if (kind === 'intake_task') {
          const status = String(targetRow.status ?? 'open');
          target = {
            available: true,
            canonical_status: status,
            terminal: status === 'done' || status === 'cancelled',
            complete: status === 'done',
          };
        } else if (kind === 'fleet_launch') {
          const state = String(targetRow.state ?? 'unknown');
          target = {
            available: true,
            canonical_status: state,
            terminal: ['exited', 'failed', 'cancelled'].includes(state),
            complete: state === 'exited',
          };
        } else {
          const status = String(targetRow.status ?? 'open');
          target = {
            available: true,
            canonical_status: status,
            terminal:
              ['accepted', 'failed', 'cancelled'].includes(status) ||
              (status === 'done' && kind !== 'review'),
            complete:
              status === 'accepted' || (kind !== 'review' && status === 'done'),
          };
        }
      } else {
        target = {
          available: false,
          canonical_status: null,
          terminal: false,
          complete: false,
        };
      }
    }

    return {
      id: link.id,
      link_id: link.id,
      conversation_id: link.conversation_id,
      kind,
      ref: link.ref,
      target,
      facts: {
        owner_acceptance: ownerAcceptance,
        first_action: firstAction,
        due_obligation: obligationResult.fact,
        independent_review: independentReview,
        accepted_outcome: acceptedOutcome,
        human_delivery: humanDelivery,
      },
      overdue_obligation: obligationResult.overdue,
      summary: {
        ready,
        complete,
        blocked,
      },
    };
  }

  private deriveOwnerAcceptanceFact(task: any, job: any): FollowThroughFact {
    if (task && Array.isArray(task.history)) {
      for (let i = task.history.length - 1; i >= 0; i--) {
        const e = task.history[i];
        if (e?.kind === 'owner_acceptance' && e.authority === 'canonical_owner_acceptance') {
          if (task.owner && e.actor !== task.owner) {
            return {
              fact_type: 'owner_acceptance',
              state: 'unknown',
              source_identity: e.actor || null,
              authority: 'unverified_owner_acceptance_actor_mismatch',
              observation: {
                claimed_actor: e.actor,
                task_owner: task.owner,
                notice: 'Operator or third-party on-behalf claim is not an owner ACK',
              },
              revision: task.version ?? null,
            };
          }
          const state: FactState = e.verdict === 'refused' ? 'refused' : 'known';
          return {
            fact_type: 'owner_acceptance',
            state,
            source_identity: e.actor || null,
            authority: 'canonical_owner_acceptance',
            observation: {
              accepted_at: e.captured_at,
              text: e.text || null,
              verdict: e.verdict || 'accepted',
            },
            revision: task.version ?? null,
          };
        }
      }
      if (typeof task.owner === 'string' && task.owner.trim()) {
        return {
          fact_type: 'owner_acceptance',
          state: 'unknown',
          source_identity: null,
          authority: 'unverified_task_data',
          observation: {
            claimed_owner_text: task.owner,
            notice: 'Mere task owner text is not acceptance',
          },
          revision: task.version ?? null,
        };
      }
    }

    if (job) {
      return {
        fact_type: 'owner_acceptance',
        state: 'unknown',
        source_identity: null,
        authority: 'unverified_job_actor',
        observation: {
          notice: 'Routing job actor is not owner acceptance',
        },
        revision: null,
      };
    }

    return {
      fact_type: 'owner_acceptance',
      state: 'unknown',
      source_identity: null,
      authority: 'none',
      observation: null,
      revision: null,
    };
  }

  private deriveFirstActionFact(
    task: any,
    job: any,
    launch: any,
    conversationId?: string,
  ): FollowThroughFact {
    // 1. Genuine owner-authenticated Hub work action / first action recorded on canonical Intake task
    if (task && Array.isArray(task.history)) {
      for (let i = task.history.length - 1; i >= 0; i--) {
        const e = task.history[i];
        if (
          (e?.kind === 'first_action' || e?.kind === 'work_action') &&
          e.authority === 'canonical_hub_action_receipt'
        ) {
          return {
            fact_type: 'first_action',
            state: 'known',
            source_identity: e.actor || null,
            authority: 'canonical_hub_action_receipt',
            observation: {
              first_action_at: e.captured_at,
              action_type: e.action_type || 'work_action',
              text: e.text || null,
              hub_action: true,
            },
            revision: task.version ?? null,
          };
        }
      }
    }

    // 2. Conversation work message validation:
    // Real linked target, current owner sender, current credential/member/grant, and causal acceptance ordering
    let unverifiedMessageNotice: string | null = null;
    if (task && conversationId) {
      const isLinked =
        this.sql
          .exec(
            "SELECT 1 FROM conversation_links WHERE conversation_id = ? AND kind = 'intake_task' AND ref = ? LIMIT 1",
            conversationId,
            task.id,
          )
          .toArray().length > 0;
      if (isLinked) {
        const ownerAcceptance = this.deriveOwnerAcceptanceFact(task, job);
        if (ownerAcceptance.state === 'known' && ownerAcceptance.observation?.accepted_at) {
          const acceptedAt = Number(ownerAcceptance.observation.accepted_at);
          const messages = this.sql
            .exec(
              'SELECT * FROM conversation_messages WHERE conversation_id = ? ORDER BY seq ASC',
              conversationId,
            )
            .toArray() as any[];
          for (const msg of messages) {
            const msgTime = Date.parse(msg.created_at);
            if (!Number.isFinite(msgTime) || msgTime < acceptedAt) {
              continue; // Causal acceptance ordering: message was before owner acceptance
            }
            if (!task.owner || msg.sender_actor !== task.owner) {
              unverifiedMessageNotice = 'Message sender does not match current task owner';
              continue;
            }
            const isMember =
              this.sql
                .exec(
                  'SELECT 1 FROM conversation_members WHERE conversation_id = ? AND actor = ? AND removed_at IS NULL LIMIT 1',
                  conversationId,
                  msg.sender_actor,
                )
                .toArray().length > 0;
            if (!isMember) {
              unverifiedMessageNotice = 'Sender is not an active conversation member';
              continue;
            }

            const cred = this.sql
              .exec(
                'SELECT * FROM credentials WHERE actor = ? AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?) ORDER BY created_at DESC LIMIT 1',
                msg.sender_actor,
                new Date(this.now()).toISOString(),
              )
              .toArray()[0] as any;
            if (!cred) {
              unverifiedMessageNotice = 'Sender credential is not active or unrevoked';
              continue;
            }

            const intake = this.getIntake();
            if (!intake) {
              unverifiedMessageNotice = 'Intake authority service unavailable';
              continue;
            }
            const caller: CallerContext = {
              actor: msg.sender_actor,
              kind: msg.sender_kind || (msg.sender_actor.startsWith('human:') ? 'human' : 'agent'),
              isAdmin: false,
              scopes: ['bus:read', 'bus:send'],
              tokenId: cred.id,
              tokenHash: cred.token_hash,
            };
            try {
              const p = intake.resolvePrincipal({ caller, scopes: caller.scopes });
              if (intake.isTaskAllowed(p, task, false)) {
                return {
                  fact_type: 'first_action',
                  state: 'known',
                  source_identity: msg.sender_actor,
                  authority: 'conversation_hub_action_receipt',
                  observation: {
                    first_action_at: msg.created_at,
                    message_id: msg.id,
                    message_seq: msg.seq,
                    sender: msg.sender_actor,
                    hub_action: true,
                    causal_acceptance_ordered: true,
                  },
                  revision: task.version ?? null,
                };
              }
            } catch {
              unverifiedMessageNotice = 'Sender does not have active intake authority for task';
            }
          }
        }
      }
    }

    // 3. Direct or joined launch with launcher_task_id correlated in jobs table
    let resolvedJobRow: any = null;
    const launcherTaskId = launch?.launcher_task_id;
    if (launcherTaskId) {
      resolvedJobRow = this.readJobByLauncherTaskId(launcherTaskId);
    }

    if (resolvedJobRow?.first_action_at) {
      return {
        fact_type: 'first_action',
        state: 'known',
        source_identity:
          resolvedJobRow.holder_host_id ||
          resolvedJobRow.holder_machine_id ||
          resolvedJobRow.actor ||
          null,
        authority: 'canonical_job_first_action',
        observation: {
          first_action_at: resolvedJobRow.first_action_at,
          evidence_digest: resolvedJobRow.evidence_digest || null,
          native_session: resolvedJobRow.native_session || null,
          launcher_task_id: resolvedJobRow.launcher_task_id || null,
        },
        revision: null,
      };
    }

    if (unverifiedMessageNotice) {
      return {
        fact_type: 'first_action',
        state: 'unknown',
        source_identity: null,
        authority: 'unverified_conversation_message',
        observation: {
          notice: unverifiedMessageNotice,
        },
        revision: task?.version ?? null,
      };
    }

    if (launch && ['starting', 'running', 'offered'].includes(String(launch.state))) {
      return {
        fact_type: 'first_action',
        state: 'unknown',
        source_identity: null,
        authority: 'unverified_launcher_start',
        observation: {
          launch_state: launch.state,
          notice: 'Launcher start is not first action',
        },
        revision: task?.version ?? null,
      };
    }

    return {
      fact_type: 'first_action',
      state: 'unknown',
      source_identity: null,
      authority: 'none',
      observation: null,
      revision: task?.version ?? null,
    };
  }

  private deriveDueObligationFact(
    task: any,
    nowMs: number,
  ): { fact: FollowThroughFact; overdue: boolean; items: ObligationItem[] } {
    if (!task || !Array.isArray(task.history)) {
      return {
        fact: {
          fact_type: 'due_obligation',
          state: 'unknown',
          source_identity: null,
          authority: 'none',
          observation: null,
          revision: null,
        },
        overdue: false,
        items: [],
      };
    }

    const items: ObligationItem[] = [];
    const resolvedRefs = new Set<string>();

    for (const e of task.history) {
      if (e?.kind === 'resolution' || e?.kind === 'approval_decision') {
        if (e.ref) resolvedRefs.add(e.ref);
      }
    }

    let hasOverdue = false;

    for (const e of task.history) {
      if (e?.kind === 'obligation') {
        const id = String(e.obligation_id || e.id);
        const responses = task.history.filter((r: any) => r?.kind === 'obligation_response' && r.ref === id);
        const latestResp = responses[responses.length - 1];
        let status: ObligationItem['status'] = 'open';
        let answeredAt: number | null = null;
        let proceededAt: number | null = null;

        for (const resp of responses) {
          if (resp?.response_type === 'proceed') {
            const t = Number(resp.captured_at ?? resp.created_at);
            if (Number.isFinite(t)) proceededAt = t;
          }
          if (resp?.response_type === 'answer') {
            const t = Number(resp.captured_at ?? resp.created_at);
            if (Number.isFinite(t)) answeredAt = t;
          }
        }

        if (latestResp) {
          status = latestResp.resulting_status || (latestResp.response_type === 'proceed' ? 'proceeded' : 'answered');
        }
        const isOverdue = status === 'open' && Number(e.deadline) <= nowMs;
        if (isOverdue) hasOverdue = true;
        items.push({
          id,
          text_hash: e.text_hash || sha256Sync(e.text || '').slice(0, 16),
          text: String(e.text || ''),
          asked_at: Number(e.asked_at ?? e.captured_at ?? e.created_at ?? 0),
          deadline: Number(e.deadline),
          wait_s: Number(e.wait_s || 1200),
          default_action: String(e.default_action || ''),
          status,
          overdue: isOverdue,
          late_reconciled: status === 'answered-after-proceeding',
          answered_at: answeredAt,
          proceeded_at: proceededAt,
          source: e.source ?? null,
        });
      } else if (
        ['followup', 'blocker', 'pending_approval'].includes(e?.kind) &&
        !resolvedRefs.has(e.id)
      ) {
        const id = String(e.id);
        const deadline = e.deadline ? Number(e.deadline) : null;
        const isOverdue = deadline !== null && deadline <= nowMs;
        if (isOverdue) hasOverdue = true;
        items.push({
          id,
          text_hash: e.text_hash || sha256Sync(e.text || '').slice(0, 16),
          text: String(e.text || ''),
          asked_at: Number(e.captured_at ?? e.created_at ?? 0),
          deadline: deadline ?? 0,
          wait_s: e.wait_s ? Number(e.wait_s) : 0,
          default_action: e.default_action ? String(e.default_action) : '',
          status: 'open',
          overdue: isOverdue,
          late_reconciled: false,
          answered_at: null,
          proceeded_at: null,
          source: e.source ?? null,
        });
      }
    }

    if (items.length === 0) {
      return {
        fact: {
          fact_type: 'due_obligation',
          state: 'unknown',
          source_identity: null,
          authority: 'none',
          observation: null,
          revision: task.version ?? null,
        },
        overdue: false,
        items: [],
      };
    }

    const hasOpen = items.some(i => i.status === 'open');
    const hasProceeded = items.some(i => i.status === 'proceeded');
    const hasLateAnswer = items.some(i => i.status === 'answered-after-proceeding');
    const allAnswered = items.every(i => i.status === 'answered' || i.status === 'answered-after-proceeding');

    const state: FactState = hasOverdue
      ? 'overdue'
      : hasOpen
        ? 'open'
        : allAnswered
          ? 'answered'
          : hasProceeded
            ? 'proceeded'
            : hasLateAnswer
              ? 'answered-after-proceeding'
              : 'known';

    return {
      fact: {
        fact_type: 'due_obligation',
        state,
        source_identity: items[0].id,
        authority: 'canonical_ask_ledger',
        observation: {
          total: items.length,
          open: items.filter(i => i.status === 'open').length,
          overdue: hasOverdue,
          default_choice: items[0]?.default_action || null,
          items,
        },
        revision: task.version ?? null,
      },
      overdue: hasOverdue,
      items,
    };
  }

  private deriveIndependentReviewFact(
    task: any,
    job: any,
    kind: string,
  ): FollowThroughFact {
    if (task && Array.isArray(task.history)) {
      for (let i = task.history.length - 1; i >= 0; i--) {
        const e = task.history[i];
        if (
          e?.kind === 'independent_review' &&
          e.authority === 'canonical_independent_review'
        ) {
          const contributors = new Set<string>();
          for (const item of task.history) {
            if (item.kind === 'commit' && item.actor) contributors.add(item.actor);
            if (item.kind === 'report' && item.actor) contributors.add(item.actor);
            if ((item.kind === 'work_action' || item.kind === 'first_action') && item.actor) contributors.add(item.actor);
            if (item.kind === 'assignment' && item.assignee) contributors.add(item.assignee);
          }
          if (typeof task.owner === 'string' && task.owner.trim()) {
            contributors.add(task.owner.trim());
          }

          const claimedReviewer = e.reviewer || e.actor;
          if (contributors.has(claimedReviewer)) {
            return {
              fact_type: 'independent_review',
              state: 'unknown',
              source_identity: null,
              authority: 'unverified_review_self_or_owner',
              observation: { notice: 'Reviewer is not independent from task implementer/contributor or owner' },
              revision: task.version ?? null,
            };
          }

          const reviewSha = String(e.source_sha || '').toLowerCase();
          let unpinnedNotice: string | null = null;
          for (let j = task.history.length - 1; j > i; j--) {
            const later = task.history[j];
            if (later?.kind === 'accepted_outcome' && later.source_sha) {
              if (String(later.source_sha).toLowerCase() !== reviewSha) {
                unpinnedNotice = 'Accepted outcome SHA does not match reviewed source SHA';
                break;
              }
            } else if (later?.kind === 'accepted_outcome' && later.outcome_ref) {
              const outcomeShaMatch = /[0-9a-f]{40}/i.exec(later.outcome_ref);
              if (outcomeShaMatch && outcomeShaMatch[0].toLowerCase() !== reviewSha) {
                unpinnedNotice = 'Accepted outcome SHA does not match reviewed source SHA';
                break;
              }
            }
            if (later?.kind === 'commit' && later.reference) {
              const commitShaMatch = /[0-9a-f]{40}/i.exec(later.reference);
              if (commitShaMatch && commitShaMatch[0].toLowerCase() !== reviewSha) {
                unpinnedNotice = 'Subsequent commit SHA does not match reviewed source SHA';
                break;
              }
            }
          }

          if (unpinnedNotice) {
            return {
              fact_type: 'independent_review',
              state: 'unknown',
              source_identity: claimedReviewer,
              authority: 'stale_review_unpinned',
              observation: {
                review_id: e.id,
                source_sha: reviewSha,
                verdict: e.verdict,
                notice: unpinnedNotice,
              },
              revision: task.version ?? null,
            };
          }

          const state: FactState =
            e.verdict === 'approved'
              ? 'known'
              : e.verdict === 'changes_requested'
                ? 'refused'
                : e.verdict === 'rejected'
                  ? 'refused'
                  : 'unknown';

          return {
            fact_type: 'independent_review',
            state,
            source_identity: claimedReviewer,
            authority: 'canonical_independent_review',
            observation: {
              review_id: e.id,
              source_sha: reviewSha,
              verdict: e.verdict,
              text: e.text || null,
              reviewed_at: e.captured_at,
              independent: true,
            },
            revision: task.version ?? null,
          };
        }
      }
    }

    if (kind === 'review' || (job && kind === 'routing_job')) {
      return {
        fact_type: 'independent_review',
        state: 'unknown',
        source_identity: null,
        authority: 'unverified_routing_job',
        observation: {
          notice: 'A routing job under the label review is not independent review',
        },
        revision: null,
      };
    }

    return {
      fact_type: 'independent_review',
      state: 'unknown',
      source_identity: null,
      authority: 'none',
      observation: null,
      revision: task?.version ?? null,
    };
  }

  private deriveAcceptedOutcomeFact(
    task: any,
    _job: any,
    launch: any,
  ): FollowThroughFact {
    if (task && Array.isArray(task.history)) {
      for (let i = task.history.length - 1; i >= 0; i--) {
        const e = task.history[i];
        if (
          e?.kind === 'accepted_outcome' &&
          e.authority === 'canonical_accepted_outcome'
        ) {
          const outcomeSha = String(e.source_sha || '').toLowerCase();
          let staleNotice: string | null = null;
          for (let j = i + 1; j < task.history.length; j++) {
            const later = task.history[j];
            if (later.kind === 'commit' && later.reference && later.reference.toLowerCase() !== outcomeSha) {
              staleNotice = 'Subsequent commit changed source after accepted outcome';
              break;
            }
            if (later.kind === 'independent_review' && later.verdict !== 'approved') {
              staleNotice = 'Subsequent review was not approved';
              break;
            }
          }
          if (staleNotice) {
            return {
              fact_type: 'accepted_outcome',
              state: 'unknown',
              source_identity: e.actor || null,
              authority: 'stale_outcome_source_changed',
              observation: {
                outcome_ref: e.outcome_ref,
                source_sha: e.source_sha,
                review_ref: e.review_ref,
                notice: staleNotice,
              },
              revision: task.version ?? null,
            };
          }

          const state: FactState =
            e.verdict === 'accepted'
              ? 'known'
              : e.verdict === 'rejected'
                ? 'refused'
                : 'unknown';
          return {
            fact_type: 'accepted_outcome',
            state,
            source_identity: e.actor || null,
            authority: 'canonical_accepted_outcome',
            observation: {
              outcome_id: e.id,
              outcome_ref: e.outcome_ref,
              source_sha: e.source_sha,
              review_ref: e.review_ref,
              verdict: e.verdict,
              text: e.text || null,
              accepted_at: e.captured_at,
            },
            revision: task.version ?? null,
          };
        }
        if (e?.kind === 'resolution' && e.authority === 'operator_record') {
          return {
            fact_type: 'accepted_outcome',
            state: 'known',
            source_identity: e.actor || null,
            authority: 'operator_resolution',
            observation: {
              text: e.text || null,
              resolved_at: e.captured_at,
            },
            revision: task.version ?? null,
          };
        }
      }
    }

    if (launch && ['exited', 'done'].includes(String(launch.state))) {
      return {
        fact_type: 'accepted_outcome',
        state: 'unknown',
        source_identity: null,
        authority: 'unverified_process_exit',
        observation: {
          exit_state: launch.state,
          notice: 'Process exit is not outcome acceptance',
        },
        revision: task?.version ?? null,
      };
    }

    return {
      fact_type: 'accepted_outcome',
      state: 'unknown',
      source_identity: null,
      authority: 'none',
      observation: null,
      revision: task?.version ?? null,
    };
  }

  private deriveHumanDeliveryFact(
    task: any,
    conversationId: string,
  ): FollowThroughFact {
    if (task && Array.isArray(task.history)) {
      for (let i = task.history.length - 1; i >= 0; i--) {
        const e = task.history[i];
        if (
          e?.kind === 'human_delivery' &&
          e.authority === 'human_delivery_receipt'
        ) {
          const recipient = e.recipient || e.actor;
          if (typeof recipient !== 'string' || !recipient.startsWith('human:')) {
            return {
              fact_type: 'human_delivery',
              state: 'unknown',
              source_identity: null,
              authority: 'unverified_delivery_recipient',
              observation: { notice: 'Delivery recipient is not an authenticated human actor' },
              revision: task.version ?? null,
            };
          }

          // Check binding to accepted outcome:
          const hasMatchingOutcome = task.history.some(
            (o: any) =>
              o.kind === 'accepted_outcome' &&
              o.verdict === 'accepted' &&
              (o.id === e.accepted_outcome_id || (e.source_sha && o.source_sha === e.source_sha)),
          );
          if (!hasMatchingOutcome) {
            return {
              fact_type: 'human_delivery',
              state: 'unknown',
              source_identity: null,
              authority: 'unbound_delivery_no_accepted_outcome',
              observation: { notice: 'Delivery receipt does not bind to an accepted outcome' },
              revision: task.version ?? null,
            };
          }

          // Stale delivery check:
          // Preserve positive valid ACK after its own CAS append:
          // Check if any modifying events occurred AFTER the human_delivery event
          let isStaleAfter = false;
          for (let j = i + 1; j < task.history.length; j++) {
            const later = task.history[j];
            if (later.kind === 'commit' || later.kind === 'accepted_outcome' || later.kind === 'independent_review') {
              isStaleAfter = true;
              break;
            }
          }
          if (isStaleAfter) {
            return {
              fact_type: 'human_delivery',
              state: 'unknown',
              source_identity: null,
              authority: 'stale_delivery_unmatched_version',
              observation: {
                receipt_version: e.task_version,
                current_version: task.version,
                notice: 'Delivery receipt references stale task version modified after delivery',
              },
              revision: task.version ?? null,
            };
          }

          return {
            fact_type: 'human_delivery',
            state: 'known',
            source_identity: recipient,
            authority: 'human_delivery_receipt',
            observation: {
              recipient,
              delivered_by: e.delivered_by || e.actor,
              accepted_outcome_id: e.accepted_outcome_id || null,
              source_sha: e.source_sha || null,
              task_version: e.task_version ?? task.version ?? null,
              acknowledgement: e.text || null,
              delivered_at: e.captured_at,
            },
            revision: task.version ?? null,
          };
        }
      }
    }

    const members = this.sql
      .exec(
        `SELECT actor, kind, last_read_seq FROM conversation_members
         WHERE conversation_id = ? AND kind = 'human' AND last_read_seq > 0`,
        conversationId,
      )
      .toArray() as any[];

    if (members.length > 0) {
      return {
        fact_type: 'human_delivery',
        state: 'unknown',
        source_identity: null,
        authority: 'unverified_read_cursor',
        observation: {
          notice: 'Read cursor alone is not task delivery',
        },
        revision: task?.version ?? null,
      };
    }

    return {
      fact_type: 'human_delivery',
      state: 'unknown',
      source_identity: null,
      authority: 'none',
      observation: null,
      revision: task?.version ?? null,
    };
  }

  public countOverdueObligationsForConversation(
    conversationId: string,
    nowMs: number = this.now(),
    targetActor?: string,
    targetCredentialId?: string | null,
  ): number {
    const links = this.sql
      .exec(
        `SELECT ref FROM conversation_links
         WHERE conversation_id = ? AND kind = 'intake_task'`,
        conversationId,
      )
      .toArray() as any[];

    if (links.length === 0) return 0;

    const intake = this.getIntake();
    if (!intake) {
      throw new Error('target_authority_unavailable');
    }

    // If targetActor is specified, establish canonical authority via IntakeService
    let authObj: any = null;
    if (targetActor) {
      const parsedHuman = parseCanonicalHumanActor(targetActor);
      if (parsedHuman) {
        const user = this.sql
          .exec('SELECT is_admin FROM users WHERE sub = ?', parsedHuman.sub)
          .toArray()[0] as any;
        if (!user || user.is_admin !== 1) {
          throw new Error('target_authority_unavailable');
        }
        let caller: CallerContext;
        if (targetCredentialId) {
          const cred = this.sql
            .exec('SELECT * FROM credentials WHERE id = ?', targetCredentialId)
            .toArray()[0] as any;
          if (
            !cred ||
            cred.kind !== 'human' ||
            cred.revoked_at ||
            !Number.isFinite(Date.parse(cred.expires_at)) ||
            Date.parse(cred.expires_at) <= nowMs
          ) {
            throw new Error('target_authority_unavailable');
          }
          caller = {
            actor: parsedHuman.actor,
            kind: 'human',
            isAdmin: true,
            scopes: ['admin', 'bus:read', 'bus:send'],
            tokenId: cred.id,
            tokenHash: cred.token_hash,
          };
        } else {
          caller = {
            actor: parsedHuman.actor,
            kind: 'human',
            isAdmin: true,
            scopes: ['admin', 'bus:read', 'bus:send'],
          };
        }
        try {
          authObj = intake.resolvePrincipal({ caller, scopes: caller.scopes });
        } catch {
          throw new Error('target_authority_unavailable');
        }
      } else {
        // Agent target actor: must validate exact bound targetCredentialId
        if (!targetCredentialId) {
          throw new Error('target_authority_unavailable');
        }
        const cred = this.sql
          .exec('SELECT * FROM credentials WHERE id = ?', targetCredentialId)
          .toArray()[0] as any;
        if (
          !cred ||
          cred.kind !== 'agent' ||
          cred.revoked_at ||
          !Number.isFinite(Date.parse(cred.expires_at)) ||
          Date.parse(cred.expires_at) <= nowMs
        ) {
          throw new Error('target_authority_unavailable');
        }
        const boundActor = credentialActor(this.sql as any, cred);
        if (!boundActor || boundActor !== targetActor) {
          throw new Error('target_authority_unavailable');
        }
        const caller: CallerContext = {
          actor: boundActor,
          kind: 'agent',
          isAdmin: false,
          scopes: ['bus:read', 'bus:send'],
          tokenId: cred.id,
          tokenHash: cred.token_hash,
        };
        try {
          authObj = intake.resolvePrincipal({ caller, scopes: caller.scopes });
        } catch {
          throw new Error('target_authority_unavailable');
        }
      }
    }

    let overdueCount = 0;
    for (const l of links) {
      const task = intake.getTaskById(l.ref);
      if (!task) continue;

      if (authObj) {
        const allowed = intake.isTaskAllowed(authObj, task, false);
        if (!allowed) {
          throw new Error('target_authority_unavailable');
        }
      }

      const derivation = this.deriveDueObligationFact(task, nowMs);
      if (derivation.overdue) {
        overdueCount += derivation.items.filter(i => i.overdue).length || 1;
      }
    }
    return overdueCount;
  }

  private readRow(table: string, id: string): any | null {
    try {
      return (
        (this.sql
          .exec(`SELECT * FROM ${table} WHERE id = ?`, id)
          .toArray()[0] as any) ?? null
      );
    } catch {
      return null;
    }
  }

  private readJobByLauncherTaskId(launcherTaskId: string): any | null {
    if (!launcherTaskId) return null;
    try {
      return (
        (this.sql
          .exec(
            'SELECT * FROM jobs WHERE launcher_task_id = ? ORDER BY created_at DESC',
            launcherTaskId,
          )
          .toArray()[0] as any) ?? null
      );
    } catch {
      return null;
    }
  }
}
