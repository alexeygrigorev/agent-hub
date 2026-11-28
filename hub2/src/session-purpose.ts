import { HttpError } from './http';

export type SessionPurpose = 'fix' | 'implement' | 'review' | 'think' | 'orchestrate';

export function sessionPurpose(value: unknown): SessionPurpose {
  if (value === undefined) return 'implement';
  if (value === 'fix' || value === 'implement' || value === 'review' || value === 'think' || value === 'orchestrate') return value;
  throw new HttpError('invalid_purpose', 400);
}

export function allowsAstra(purpose: SessionPurpose): boolean {
  return purpose === 'review' || purpose === 'think';
}

export function isOrchestratorLaunch(launch: {
  interactive?: unknown;
  with_goal?: unknown;
  purpose?: unknown;
  engine?: unknown;
}): boolean {
  if (String(launch.engine ?? '').toLowerCase() === 'shell') return false;
  return launch.interactive === true || launch.with_goal === true || launch.purpose === 'orchestrate';
}
