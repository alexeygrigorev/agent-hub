/** Resource management knobs (docs/resource-management.md, intake 4fcd3139).
 * Floor lowered 10 GiB -> 2 GiB per Alexey t1476u (2026-10-26). */
export const MEMORY_FLOOR_MB = 2048;
export const NEXT_SLOT_MB = 1500;
/** CPU guard: no new placement while 5-min load > factor x cores (Alexey t1475u).
 * Raised 1.5 -> 3.0 (Alexey t1510u/t1511u, 2026-10-26): loadavg is inflated by
 * D-state/fork churn while CPU was ~50% idle; run heavy, throttle only on choking. */
export const CPU_LOAD_FACTOR = 3.0;
