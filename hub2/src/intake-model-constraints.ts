import {HttpError} from './http';

/** Semantic model names, never mutable policy-row IDs or picker weights. */
export type ModelConstraint = {
  models_by_engine?: Record<string,string[]>;
  efforts?: string[];
};
export type InheritedModelConstraint = {intake_id:string; constraint:ModelConstraint};
type Parent = {id:string; parent_intake_id?:string|null; source_json?:string|null};
const EFFORTS = new Set(['low','medium','high','max','unspecified']);

function invalid():never {throw new HttpError('invalid_model_constraints',400);}
function record(value:unknown):value is Record<string,unknown> {
  return typeof value==='object'&&value!==null&&!Array.isArray(value);
}
function names(value:unknown, pattern:RegExp, maximum:number):string[] {
  if(!Array.isArray(value)||!value.length||value.length>maximum
    ||value.some(item=>typeof item!=='string'||!pattern.test(item)))return invalid();
  return [...new Set((value as string[]).map(item=>item.toLowerCase()))].sort();
}

export function normalizeModelConstraint(value:unknown):ModelConstraint|null {
  if(value===null)return null;
  if(!record(value)||!Object.keys(value).length||Object.keys(value).some(
    key=>!['models_by_engine','efforts'].includes(key)))return invalid();
  const result:ModelConstraint={};
  if(value.models_by_engine!==undefined){
    const input=value.models_by_engine;
    if(!record(input)||!Object.keys(input).length||Object.keys(input).length>8)return invalid();
    const rows:Array<[string,string[]]>=[];
    const seen=new Set<string>();
    for(const key of Object.keys(input).sort()){
      if(!/^[A-Za-z0-9_.-]{1,48}$/.test(key)||seen.has(key.toLowerCase()))return invalid();
      seen.add(key.toLowerCase());
      rows.push([key.toLowerCase(),names(input[key],/^[A-Za-z0-9][A-Za-z0-9_.+/-]{0,127}$/,8)]);
    }
    result.models_by_engine=Object.fromEntries(rows.sort(([a],[b])=>a.localeCompare(b)));
  }
  if(value.efforts!==undefined){
    result.efforts=names(value.efforts,/^[a-z]+$/,5);
    if(result.efforts.some(effort=>!EFFORTS.has(effort)))return invalid();
  }
  if(!Object.keys(result).length)return invalid();
  return result;
}

/** Every ancestor remains an independent restriction: children cannot widen it. */
export function inheritedModelConstraints(id:string, read:(id:string)=>Parent|undefined):InheritedModelConstraint[] {
  const seen=new Set<string>(),result:InheritedModelConstraint[]=[];
  while(id){
    if(seen.has(id)||seen.size>=32)throw new HttpError('intake_model_constraints_ancestry_invalid',409);
    seen.add(id);
    const row=read(id);
    if(!row||row.id!==id)throw new HttpError('intake_model_constraints_ancestor_missing',409);
    try{
      const source:unknown=JSON.parse(row.source_json||'{}');
      if(!record(source))throw new Error('invalid_source');
      if(Object.hasOwn(source,'model_constraints')){
        const constraint=normalizeModelConstraint(source.model_constraints);
        if(constraint)result.push({intake_id:id,constraint});
      }
    }catch{throw new HttpError('intake_model_constraints_invalid',503);}
    id=row.parent_intake_id??'';
  }
  return result;
}

export function modelConstraintsAllow(candidate:{engine:string;model:string;effort?:string}, rules:readonly ModelConstraint[]):boolean {
  return rules.every(rule=>{
    const engine=candidate.engine.toLowerCase();
    const models=rule.models_by_engine&&Object.hasOwn(rule.models_by_engine,engine)
      ?rule.models_by_engine[engine]:undefined;
    return (!models||models.includes(candidate.model.toLowerCase()))
      &&(!rule.efforts||rule.efforts.includes(candidate.effort?.toLowerCase()??'unspecified'));
  });
}
