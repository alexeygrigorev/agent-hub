import type { DurableObjectNamespace } from '@cloudflare/workers-types';

export type ActorKind = 'operator' | 'machine' | 'agent';

export interface Actor {
  id: string;
  kind: ActorKind;
  scopes: string[];
  resource_id?: string;
  generation?: string;
  family_id?: string;
  client_id?: string;
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
