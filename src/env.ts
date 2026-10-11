import { Hub } from './hub';

export interface Env {
  HUB: DurableObjectNamespace<Hub>;
  
  // Auth Config
  AUTH_BASE_URL: string;
  AUTH_CLIENT_ID: string;
  AUTH_CALLBACK_URL: string;
  AUTH_ISSUER: string;
  AUTH_JWKS_URL: string;
  HUB_ADMINS: string;
  SESSION_SECRET: string;
}
