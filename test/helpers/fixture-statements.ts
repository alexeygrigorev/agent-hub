import { fixtureSql } from './fixture-budget';

const statements=new WeakMap<object,any>();
/** Keep the original handle for real service/function calls. Only explicit
 * synthetic fixture SQL statements use this separate native, watched handle. */
export function registerFixtureStatements(hub:any,state:any=hub.ctx):void {
  statements.set(hub.sql,fixtureSql(hub,state));
}
export function registerFixtureStatementAlias(alias:object,hub:any,state:any=hub.ctx):void {
  statements.set(alias,fixtureSql(hub,state));
}
export function fixtureStatements(sql:any):any{return statements.get(sql)??sql;}
