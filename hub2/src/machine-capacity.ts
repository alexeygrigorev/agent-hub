/**
 * Machine session capacity. A machine hosts several sessions at once; the
 * operator override arrives as a heartbeat label, and heartbeat labels are
 * string lists, so it travels as `capacity:8`. The object form stays accepted
 * for direct row writes. Without an override a machine hosts 3 sessions, and
 * no override can exceed the fleet bound.
 */
export const DEFAULT_MACHINE_CAPACITY = 3;
export const MAX_MACHINE_CAPACITY = 8;

export function parseCapacityLabel(label:string):number|null{
  const match=/^capacity[:=](\d+)$/.exec(label);
  if(!match)return null;
  const n=Number(match[1]);
  return Number.isInteger(n)&&n>=1?Math.min(n,MAX_MACHINE_CAPACITY):null;
}

export function machineCapacity(labelsJson:string):number{
  try{
    const labels=JSON.parse(labelsJson);
    const n=Number((labels as {capacity?:unknown}|null)?.capacity);
    if(Number.isInteger(n)&&n>=1)return Math.min(n,MAX_MACHINE_CAPACITY);
    if(Array.isArray(labels))for(const label of labels)if(typeof label==='string'){const parsed=parseCapacityLabel(label);if(parsed!==null)return parsed;}
  }catch{}
  return DEFAULT_MACHINE_CAPACITY;
}
