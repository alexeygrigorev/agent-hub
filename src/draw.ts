/**
 * Seeded weighted random selection for unpinned auto-starts (intake 9647e6d5).
 *
 * A plain `hub sessions start` names no engine/model, so placement must not
 * deterministically stack every start on the single highest-scoring engine
 * (Codex luna). Instead the winner is drawn at random with weight
 * proportional to each candidate's remaining quota headroom, so engines with
 * headroom (Claude sonnet, Antigravity, Z.ai, OpenCode muse, Codex luna) all
 * get starts over time and no fixed order emerges.
 *
 * Randomness is always injected as `rand: () => number`: production passes
 * `Math.random`, tests pass `mulberry32(seed)`. Pinned requests (engine or
 * model override), previews and the recommend/rebalance paths never draw and
 * keep the deterministic top-score pick.
 */

/** Deterministic PRNG (mulberry32) for seeded tests. `rand()` returns [0, 1). */
export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * Draw an index with probability proportional to `weights`. Non-finite or
 * non-positive weights count as zero; when every weight is zero the first
 * index wins (same as the deterministic top pick). Pure and total: a single
 * `rand()` call per draw.
 */
export function weightedDrawIndex(weights: number[], rand: () => number): number {
  let total = 0;
  for (const w of weights) total += Number.isFinite(w) && w > 0 ? w : 0;
  if (!(total > 0) || weights.length === 0) return 0;
  let r = rand() * total;
  for (let i = 0; i < weights.length; i++) {
    const w = Number.isFinite(weights[i]) && weights[i] > 0 ? weights[i] : 0;
    r -= w;
    if (r < 0) return i;
  }
  return weights.length - 1;
}
