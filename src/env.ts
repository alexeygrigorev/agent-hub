import { Hub } from './hub';

export interface Env {
  // Type-safe DurableObjectNamespace binding for Hub Durable Object
  HUB: DurableObjectNamespace<Hub>;
  
  // Auth Config
  AUTH_BASE_URL: string;
  AUTH_CLIENT_ID: string;
  AUTH_CALLBACK_URL: string;
  AUTH_ISSUER: string;
  AUTH_JWKS_URL: string;
  HUB_ADMINS: string;
  SESSION_SECRET: string;
  // Optional: SHA-256 hex of a high-entropy admin sign-in token (POST /auth/token)
  HUB_ADMIN_TOKEN_SHA256?: string;

  // Liveness and alerts (optional)
  HUB_ALERT_TO?: string;
  HUB_ALERT_WEBHOOK?: string;
  HUB_MACHINE_STALE_S?: string;
  HUB_MACHINE_OFFLINE_S?: string;
  HUB_AGENT_STALL_S?: string;
  HUB_HEARTBEAT_STALE_S?: string;
  HUB_LAUNCH_STUCK_S?: string;
}
