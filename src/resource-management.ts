/** Resource management knobs (docs/resource-management.md, intake 4fcd3139).
 * Floor lowered 10 GiB -> 2 GiB per Alexey t1476u (2026-10-26). */
export const MEMORY_FLOOR_MB = 2048;
export const NEXT_SLOT_MB = 1500;
/** CPU guard: no new placement while 5-min load > factor x cores (Alexey t1475u). */
export const CPU_LOAD_FACTOR = 1.5;
