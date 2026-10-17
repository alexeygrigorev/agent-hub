import { Hub as BaseHub } from './hub';
import type { ActionDeps } from './checks';
import { Env } from './env';
import { RETENTION_CLEANUP_CURSOR_SQL, runFairRetentionPass } from './bus';
import {
  ConversationsService,
  authenticateConversationRequest,
} from './conversations';

/**
 * Durable Object subclass extending BaseHub with the M1 `/api/conversations`
 * domain service while preserving the `Hub` Durable Object export name,
 * SQLite storage authority (`this.sql` + `this.ctx.storage.transactionSync`),
 * and all inherited fetch, RPC (`scheduledTick`, `runChecks`), alarm, and
 * WebSocket hibernation behavior.
 */
export class Hub extends BaseHub {
  public conversationsService: ConversationsService;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    const storage = this.ctx.storage as DurableObjectStorage & {
      transactionSync?: <T>(fn: () => T) => T;
    };
    const rawTx =
      typeof storage.transactionSync === 'function'
        ? <T>(fn: () => T): T => storage.transactionSync!(fn)
        : undefined;
    // Child BusService/ConversationsService also call transactionSync. Nested
    // BEGIN is unsupported here, so depth>0 joins the open outer transaction.
    let txnDepth = 0;
    const txSync = <T>(fn: () => T): T => {
      if (!rawTx || txnDepth > 0) return fn();
      txnDepth += 1;
      try {
        return rawTx(fn);
      } finally {
        txnDepth -= 1;
      }
    };
    this.busService.txSync = txSync;
    this.conversationsService = new ConversationsService(this.sql, txSync);
    this.conversationsService.init();

    const baseRetentionCleanup = this.busService.retentionCleanup.bind(
      this.busService,
    );
    this.busService.retentionCleanup = (
      now: number = Date.now(),
      limit: number = 100,
    ) => {
      const raw = Math.floor(Number(limit));
      const normalizedLimit = Number.isFinite(raw)
        ? Math.max(1, Math.min(500, raw))
        : 100;
      const emptyConv = {
        conversationsDeleted: 0,
        messagesDeleted: 0,
        removedMembersDeleted: 0,
        membersDeleted: 0,
        rowsDeleted: 0,
        hasMore: false,
      };
      const emptyBus = {
        messagesDeleted: 0,
        claimsDeleted: 0,
        hasMore: false,
      };

      this.sql.exec(RETENTION_CLEANUP_CURSOR_SQL);
      return txSync(() => {
        let remaining = normalizedLimit;
        let busCleanup = emptyBus;
        let convCleanup = emptyConv;

        const takeBus = (budget: number): number => {
          if (budget <= 0 || remaining <= 0) return 0;
          if (!this.busService.hasExpiredRows(now)) return 0;
          const step = baseRetentionCleanup(now, Math.min(remaining, budget));
          const deleted =
            Number(step.messagesDeleted ?? 0) + Number(step.claimsDeleted ?? 0);
          busCleanup = {
            messagesDeleted:
              Number(busCleanup.messagesDeleted ?? 0) +
              Number(step.messagesDeleted ?? 0),
            claimsDeleted:
              Number(busCleanup.claimsDeleted ?? 0) +
              Number(step.claimsDeleted ?? 0),
            hasMore: step.hasMore,
          };
          remaining = Math.max(0, remaining - deleted);
          return deleted;
        };

        const takeConv = (budget: number): number => {
          if (budget <= 0 || remaining <= 0) return 0;
          if (!this.conversationsService.hasExpiredRows(now)) return 0;
          const step = this.conversationsService.retentionCleanup(
            now,
            Math.min(remaining, budget),
          );
          const deleted = Number(step.rowsDeleted ?? 0);
          convCleanup = {
            conversationsDeleted:
              convCleanup.conversationsDeleted + step.conversationsDeleted,
            messagesDeleted: convCleanup.messagesDeleted + step.messagesDeleted,
            removedMembersDeleted:
              convCleanup.removedMembersDeleted + step.removedMembersDeleted,
            membersDeleted: convCleanup.membersDeleted + step.membersDeleted,
            rowsDeleted: convCleanup.rowsDeleted + step.rowsDeleted,
            hasMore: step.hasMore,
          };
          remaining = Math.max(0, remaining - deleted);
          return deleted;
        };

        runFairRetentionPass(
          this.sql,
          'hub',
          [
            {
              hasWork: () => this.busService.hasExpiredRows(now),
              spend: takeBus,
            },
            {
              hasWork: () => this.conversationsService.hasExpiredRows(now),
              spend: takeConv,
            },
          ],
          normalizedLimit,
        );

        return {
          ...busCleanup,
          conversationsDeleted: convCleanup.conversationsDeleted,
          conversationMessagesDeleted: convCleanup.messagesDeleted,
          removedMembersDeleted: convCleanup.removedMembersDeleted,
          conversationMembersDeleted: convCleanup.membersDeleted,
          conversationRowsDeleted: convCleanup.rowsDeleted,
          hasMore:
            this.busService.hasExpiredRows(now) ||
            this.conversationsService.hasExpiredRows(now),
        };
      });
    };
  }

  override initDb(): void {
    super.initDb();
    this.conversationsService?.init();
  }

  protected override extraCheckDeps(): Partial<ActionDeps> {
    const service = this.conversationsService;
    if (!service) return {};
    return { conversation: {
      evaluateBacklog: query => service.evaluateScheduledBacklog(query),
      sendFollowUp: intent => service.postScheduledFollowUp(intent),
    } };
  }

  override async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;

    if (path === '/api/conversations' || path.startsWith('/api/conversations/')) {
      try {
        const auth = await authenticateConversationRequest(
          this.env,
          this.sql,
          request,
          () => this.conversationsService.getNow(),
        );
        return await this.conversationsService.handle(request, auth);
      } catch {
        return Response.json(
          { error: 'internal_error' },
          { status: 500, headers: { 'Cache-Control': 'private, no-store' } },
        );
      }
    }

    return super.fetch(request);
  }
}

export { Hub as ConversationHub, BaseHub };
