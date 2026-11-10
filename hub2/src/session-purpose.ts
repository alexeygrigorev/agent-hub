import { HttpError } from './http';

export type SessionPurpose = 'fix' | 'implement' | 'review' | 'think';

export function sessionPurpose(value: unknown): SessionPurpose {
  if (value === undefined) return 'implement';
  if (value === 'fix' || value === 'implement' || value === 'review' || value === 'think') return value;
  throw new HttpError('invalid_purpose', 400);
}

export function allowsAstra(purpose: SessionPurpose): boolean {
  return purpose === 'review' || purpose === 'think';
}
