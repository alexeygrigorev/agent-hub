
const WEEKDAYS: Record<string, number> = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
const FIELD_DOMAINS: [number, number][] = [[0, 59], [0, 23], [1, 31], [1, 12], [0, 7]];

export interface CronFields {
  minutes: number[];
  hours: number[];
  months: number[];
  /** null means unrestricted (`*`); standard vixie OR-rule applies when both
   * day-of-month and day-of-week are restricted. */
  doms: number[] | null;
  dows: number[] | null;
}

function parseField(raw: string, index: number): { values: number[]; restricted: boolean } | null {
  const [lo, hi] = FIELD_DOMAINS[index];
  if (!raw.trim()) return null;
  const values = new Set<number>();
  let restricted = false;
  for (const part of raw.split(',')) {
    const match = /^(\*|\d+)(?:-(\d+))?(?:\/(\d+))?$/.exec(part);
    if (!match) return null;
    const step = match[3] ? Number(match[3]) : 1;
    if (!Number.isInteger(step) || step < 1 || step > hi - lo + 1) return null;
    let start: number, end: number;
    if (match[1] === '*') {
      if (match[2] !== undefined) return null;
      start = lo; end = hi;
    } else {
      start = Number(match[1]);
      end = match[2] !== undefined ? Number(match[2]) : (match[3] !== undefined ? hi : start);
      if (match[2] === undefined && match[3] !== undefined) end = hi; // `N/step` = N-max/step
    }
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < lo || end > hi || start > end) return null;
    if (match[1] !== '*' || match[2] !== undefined) restricted = true;
    for (let v = start; v <= end; v += step) values.add(index === 4 && v === 7 ? 0 : v);
  }
  return { values: [...values].sort((a, b) => a - b), restricted };
}

/** Strict five-field cron (minute hour day-of-month month day-of-week).
 * Numbers only, `*`, lists, ranges and ` /step`; no names or macros. */
export function parseCron(expr: string): CronFields | null {
  if (typeof expr !== 'string') return null;
  const fields = expr.trim().split(/\s+/);
  if (fields.length !== 5) return null;
  const parsed: (ReturnType<typeof parseField>)[] = [];
  for (let i = 0; i < 5; i++) {
    const field = parseField(fields[i], i);
    if (!field) return null;
    parsed.push(field);
  }
  return {
    minutes: parsed[0]!.values,
    hours: parsed[1]!.values,
    doms: parsed[2]!.restricted ? parsed[2]!.values : null,
    months: parsed[3]!.values,
    dows: parsed[4]!.restricted ? parsed[4]!.values : null,
  };
}

function dayMatches(cron: CronFields, y: number, m: number, d: number, weekday: number): boolean {
  if (!cron.months.includes(m)) return false;
  const domHit = cron.doms !== null && cron.doms.includes(d);
  const dowHit = cron.dows !== null && cron.dows.includes(weekday);
  if (cron.doms !== null && cron.dows !== null) return domHit || dowHit;
  if (cron.doms !== null) return domHit;
  if (cron.dows !== null) return dowHit;
  return true;
}

interface LocalParts { y: number; m: number; d: number; h: number; min: number; weekday: number }

function localFormatter(tz: string): Intl.DateTimeFormat {
  return new Intl.DateTimeFormat('en-US', {
    timeZone: tz, year: 'numeric', month: 'numeric', day: 'numeric',
    hour: 'numeric', minute: 'numeric', hourCycle: 'h23', weekday: 'short',
  });
}

function localParts(format: Intl.DateTimeFormat, ms: number): LocalParts {
  const parts: Record<string, string> = {};
  for (const part of format.formatToParts(ms)) parts[part.type] = part.value;
  return {
    y: Number(parts.year), m: Number(parts.month), d: Number(parts.day),
    h: Number(parts.hour), min: Number(parts.minute), weekday: WEEKDAYS[parts.weekday] ?? -1,
  };
}

function asUtc(p: LocalParts): number {
  return Date.UTC(p.y, p.m - 1, p.d, p.h, p.min);
}

/** Offset (milliseconds east of UTC) of `tz` at instant `ms`. */
function tzOffsetMs(format: Intl.DateTimeFormat, ms: number): number {
  return asUtc(localParts(format, ms)) - ms;
}

/** UTC instant whose local wall-clock time in `tz` is exactly
 * y-m-d h:min, or null when that wall time does not exist (DST gap). */
function utcFromLocal(format: Intl.DateTimeFormat, y: number, m: number, d: number, h: number, min: number): number | null {
  const naive = Date.UTC(y, m - 1, d, h, min);
  let guess = naive - tzOffsetMs(format, naive);
  for (let i = 0; i < 3; i++) {
    const refined = naive - tzOffsetMs(format, guess);
    if (refined === guess) break;
    guess = refined;
  }
  const check = localParts(format, guess);
  if (!(check.y === y && check.m === m && check.d === d && check.h === h && check.min === min)) return null;
  // A fall-back repeat maps two instants to the same wall clock; the fixed
  // point above lands on the later one, so prefer the earlier instantiation
  // when it shows the same wall time (conventional cron behavior).
  const earlier = guess - 3600000;
  const prev = localParts(format, earlier);
  if (prev.y === y && prev.m === m && prev.d === d && prev.h === h && prev.min === min) return earlier;
  return guess;
}

/** First instant strictly after `afterMs` matching the cron in `tz`, scanning
 * forward at most 1490 local days (covers the four-year leap cycle). */
export function cronNext(cron: CronFields, tz: string, afterMs: number, format?: Intl.DateTimeFormat): number | null {
  const fmt = format ?? localFormatter(tz);
  const start = Math.floor(afterMs / 60000) * 60000 + 60000; // first whole minute strictly after
  const first = localParts(fmt, start);
  // Step local days by UTC noon: noon-to-noon always crosses exactly one local
  // midnight, so the local date advances by exactly one day per step.
  let noon = Date.UTC(first.y, first.m - 1, first.d, 12);
  for (let day = 0; day < 1490; day++, noon += 86400000) {
    const dayParts = day === 0 ? first : localParts(fmt, noon);
    if (!dayMatches(cron, dayParts.y, dayParts.m, dayParts.d, dayParts.weekday)) continue;
    for (const h of cron.hours) {
      for (const min of cron.minutes) {
        // Physical verification decides, never wall-clock ordering: a spring
        // gap yields null here, a fall-back repeat resolves to its first
        // instantiation, and on DST days wall order can disagree with instant
        // order, so only `candidate > afterMs` may accept a fire.
        const candidate = utcFromLocal(fmt, dayParts.y, dayParts.m, dayParts.d, h, min);
        if (candidate !== null && candidate > afterMs) return candidate;
      }
    }
  }
  return null;
}

export function validTimezone(tz: string): boolean {
  try { localFormatter(tz); return true; } catch { return false; }
}
