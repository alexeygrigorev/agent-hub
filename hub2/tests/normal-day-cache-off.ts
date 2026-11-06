// Test-only cache comparator; no production switch or source mutation.
import {ReadCache as ActualReadCache} from '../src/read-cache';
export {assertCacheSchema} from '../src/read-cache';
export class ReadCache extends ActualReadCache {
  get(_sql:string,_bindings:unknown[]):unknown[]|undefined{return undefined;}
  put(_sql:string,_bindings:unknown[],_rows:unknown[]):void{}
}
