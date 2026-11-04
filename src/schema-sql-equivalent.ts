/** Compare generated SQLite DDL without changing quoted values or identifiers.
 * SQLite omits IF NOT EXISTS from its stored CREATE definition. Everything
 * else must match token-for-token; unknown syntax conservatively upgrades. */
export function schemaSqlEquivalent(actual:string,expected:string):boolean {
 const tokens=(sql:string):string[]=>{
  const parts=sql.match(/--[^\n]*(?:\n|$)|\/\*[\s\S]*?\*\/|'(?:''|[^'])*'|"(?:""|[^"])*"|`(?:``|[^`])*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9]*|\s+|./g)??[];
  const result=parts.filter(token=>!/^\s+$/.test(token)&&!token.startsWith('--')&&!token.startsWith('/*'))
   .map(token=>/^[A-Za-z_][A-Za-z_0-9]*$/.test(token)?token.toLowerCase():token);
  const create=result.indexOf('create');
  // Only remove the CREATE object's optional existence clause, never a
  // coincidentally similar sequence in its trigger body.
  if(create===0&&['trigger','index'].includes(result[1])&&result.slice(2,5).join(' ')==='if not exists')result.splice(2,3);
  if(result.at(-1)===';')result.pop();
  return result;
 };
 return JSON.stringify(tokens(actual))===JSON.stringify(tokens(expected));
}
