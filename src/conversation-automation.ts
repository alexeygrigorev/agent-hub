/** Internal-only provenance. This address is reserved by conversation/registry auth. */
export const AUTOMATION_SENDER = 'system:conversation-checks';
export const AUTOMATION_SCHEMA = `
CREATE TABLE IF NOT EXISTS conversation_automation_grants (
  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, check_name TEXT NOT NULL,
  target_actor TEXT NOT NULL, target_credential_id TEXT, owner_sub TEXT NOT NULL,
  expires_at INTEGER NOT NULL, body_template TEXT NOT NULL, action_on TEXT NOT NULL,
  revoked_at INTEGER, UNIQUE(conversation_id, check_name)
);
CREATE TABLE IF NOT EXISTS conversation_automation_receipts (
  effect_key TEXT PRIMARY KEY, request_json TEXT NOT NULL, message_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL, created_at INTEGER NOT NULL
);
`;
export type BacklogQuery = { actor: string; checkName: string; maxAgeS: number; conversationId?: string };
export type FollowUpIntent = {
  actor: string; checkName: string; conversationId: string; grantId: string;
  body: string; bodyTemplate: string; on: 'fail' | 'always'; idempotencyKey: string;
};
export type ConversationCheckPort = {
  evaluateBacklog(query: BacklogQuery): { status: 'pass' | 'fail' | 'error'; detail: string };
  sendFollowUp(intent: FollowUpIntent): string | Promise<string>;
};

export const AUTOMATION_REVOKE_TRIGGER = `CREATE TRIGGER IF NOT EXISTS conversation_automation_remove_member AFTER UPDATE OF removed_at ON conversation_members
WHEN NEW.removed_at IS NOT NULL BEGIN
 UPDATE conversation_automation_grants SET revoked_at = 1 WHERE conversation_id = NEW.conversation_id
 AND (target_actor = NEW.actor OR 'human:' || owner_sub = NEW.actor);
END;
`;
