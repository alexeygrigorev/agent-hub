import type { DurableObjectNamespace } from '@cloudflare/workers-types';

export type ActorKind = 'operator' | 'machine' | 'agent' | 'service';

export interface Actor {
  id: string;
  kind: ActorKind;
  scopes: string[];
  resource_id?: string;
  generation?: string;
  family_id?: string;
  client_id?: string;
}


export const DEPLOYMENT_POLICY_SCOPES = ['model-policy:read', 'model-policy:write'] as const;
export const DEPLOYMENT_REQUEST_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
export const DEPLOYMENT_LABEL = /^[A-Za-z0-9][A-Za-z0-9 ._:-]{0,79}$/;

/** A service actor never acquires human, host, or caller-selected authority. */
export function isDeploymentActor(value: unknown): value is Actor {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const actor = value as Record<string, unknown>;
  return Object.keys(actor).every(key => ['id','kind','scopes','resource_id','generation','family_id','client_id'].includes(key))
    && actor.kind === 'service' && typeof actor.id === 'string'
    && actor.id.startsWith('deployment:') && DEPLOYMENT_REQUEST_ID.test(actor.id.slice(11))
    && actor.resource_id === 'model-policy'
    && typeof actor.generation === 'string' && DEPLOYMENT_REQUEST_ID.test(actor.generation)
    && typeof actor.family_id === 'string' && DEPLOYMENT_REQUEST_ID.test(actor.family_id)
    && typeof actor.client_id === 'string' && DEPLOYMENT_LABEL.test(actor.client_id)
    && Array.isArray(actor.scopes) && actor.scopes.length === 2
    && DEPLOYMENT_POLICY_SCOPES.every(scope => (actor.scopes as unknown[]).includes(scope));
}

/** Optional Node ingress authority; failures never authorize or refuse primary work. */
export interface CredentialMonitorEvidence {
  begin(): unknown;
  finish(identity: unknown, decision: 'alert' | 'observation' | null): Promise<void>;
  invalidate(): void;
  snapshot(nonce: string): Promise<JsonRecord>;
}

export interface Hub2Env {
  // This worker-types release brands RPC-capable DO classes. These DOs use
  // fetch-only stubs, so keep bindings unbranded instead of asserting an
  // invalid DurableObjectNamespace<Class> relationship.
  CONTROL: DurableObjectNamespace;
  PROJECTS: DurableObjectNamespace;
  MACHINES: DurableObjectNamespace;
  ACCOUNTS: DurableObjectNamespace;
  MAILBOXES: DurableObjectNamespace;
  HUB2_RELEASE_MODE?: string;
  HUB2_ALERT_EVIDENCE_DIR?: string;
  CREDENTIAL_MONITOR_EVIDENCE?: CredentialMonitorEvidence;
  HUB2_DISK_FLOOR_GB?: string;
  HUB2_SCHEMA_MIGRATIONS?: string;
  HUB_ALERT_WEBHOOK?: string;
  HUB_ALERT_WEBHOOK_TOKEN?: string;
  HUB2_BOOTSTRAP_TOKEN?: string;
  HUB2_DEV_OPERATOR_TOKEN?: string;
  HUB2_DEFAULT_ACCOUNT_ID?: string;
  HUB2_DEFAULT_PROJECT?: string;
  HUB2_ENGINE_ACCOUNTS?: string;
  HUB2_CREDENTIAL_KEY?: string;
  HUB2_DEV_METRICS?: string;
  ASSETS?: Fetcher;
}

export interface JsonRecord { [key: string]: unknown }
