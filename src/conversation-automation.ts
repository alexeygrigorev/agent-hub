/** Internal-only provenance. This address is reserved by conversation/registry auth. */
export const AUTOMATION_SENDER = 'system:conversation-checks';
// Shared by standalone ConversationsService and ChecksService. A slot is the
// authority for precisely one frozen intent, not just a scheduler cache.
export const CHECK_SLOT_SCHEMA = `
CREATE TABLE IF NOT EXISTS hub_check_slots (name TEXT PRIMARY KEY, slot_key TEXT NOT NULL, next_at INTEGER NOT NULL, check_json TEXT NOT NULL, result_json TEXT, intent_json TEXT, action TEXT, state TEXT NOT NULL, token TEXT, lease_until INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_check_slot_effect ON hub_check_slots(('check-' || slot_key));
CREATE INDEX IF NOT EXISTS idx_check_slot_grant ON hub_check_slots(json_extract(check_json, '$.action.grant_id'));
`;
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
CREATE INDEX IF NOT EXISTS idx_automation_grant_expiry ON conversation_automation_grants(expires_at);
CREATE INDEX IF NOT EXISTS idx_automation_receipt_parent ON conversation_automation_receipts(conversation_id);
` + CHECK_SLOT_SCHEMA;
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
