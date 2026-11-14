import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {MCP_ROUTES,publicTools,toolRequest} from '../compat/mcp-contract/mcp-tools.ts';
import {CLI_EXCLUSIONS,curatedTools,legacyTools,CORE_TOOL_NAMES,DOMAIN_TOOL_NAMES,DEFAULT_TOOL_EXCLUSIONS,LEGACY_MCP_TOOL_NAMES,LEGACY_MCP_ALIASES,EXTENDED_MCP_ALIASES} from '../compat/mcp-contract/mcp-curation.ts';
const commands=JSON.parse(readFileSync(new URL('../../host/tests/fixtures/cli-commands.json',import.meta.url),'utf8'));
const routed=new Set(MCP_ROUTES.flatMap(r=>r.cli));
test('EVERY actual CLI leaf has a route or documented local-only exclusion',()=>{
 for(const command of commands)assert(routed.has(command)||Boolean(CLI_EXCLUSIONS[command]),command);
});
test('native private collection remains host-local',()=>{
 assert(commands.includes('engines import-native'));
 assert(CLI_EXCLUSIONS['engines import-native'].includes('private native Claude credentials'));
 assert(!routed.has('engines import-native'));
});
test('operator-only engine controls never become model-tool mutation mappings',()=>{
 for(const command of ['engines registry list','engines registry set','engines registry remove',
 'engines default-claude enroll','engines default-claude enable-writes','engines default-claude disable']){
  assert(commands.includes(command));assert(!routed.has(command));assert.match(CLI_EXCLUSIONS[command],/Operator-only/);
 }
});

test('complete tool schemas retain budgets and fixed write annotations',()=>{
 const tools=publicTools();assert.deepEqual(tools.map(t=>t.name),MCP_ROUTES.map(r=>r.name));
 assert(Buffer.byteLength(JSON.stringify({tools}))<=52000);
 assert(Buffer.byteLength(JSON.stringify({jsonrpc:'2.0',id:1,result:{tools}}))<=52000);
 for(const route of MCP_ROUTES){
  const tool=tools.find(t=>t.name===route.name)!;const readOnly=route.method==='GET'||route.name==='sessions_capture';
  assert.deepEqual(tool.annotations,readOnly?{readOnlyHint:true,destructiveHint:false,openWorldHint:false}:{destructiveHint:true,openWorldHint:false});
  if(route.method!=='GET'){
   assert.deepEqual(tool.inputSchema.properties.payload,{type:'object',description:'JSON request body.'});
   if(route.name==='sessions_cleanup'){
    assert('query' in tool.inputSchema.properties);
    const request=toolRequest('http://example.com',route.name,{query:{host:'fixture-host',task:'fixture-task',owner:'fixture-owner'},payload:{dry_run:true}},'Bearer fixture');
    assert.deepEqual(Object.fromEntries(new URL(request.url).searchParams),{host:'fixture-host',task:'fixture-task',owner:'fixture-owner'});
   }else assert(!('query' in tool.inputSchema.properties));
   const args=Object.fromEntries([...route.path.matchAll(/\{(\w+)\}/g)].map(m=>[m[1],'fixture']));
   assert.doesNotThrow(()=>toolRequest('http://example.com',route.name,{...args,query:{},payload:{}},'Bearer fixture'));
  }
 }
});
test('curated and explicit legacy contract schemas retain exact names, counts and budgets',()=>{
 const tools=curatedTools();assert.deepEqual(tools.map(t=>t.name),[...CORE_TOOL_NAMES,...DOMAIN_TOOL_NAMES]);
 assert.equal(tools.length,25);assert(Buffer.byteLength(JSON.stringify({tools}))<=18000);
 const by=new Map(tools.map(t=>[t.name,t]));
 for(const name of ['intake_governance','bus_read','conversations_read','fleet_read'])assert.deepEqual(by.get(name)?.annotations,{readOnlyHint:true,destructiveHint:false,openWorldHint:false});
 for(const name of ['bus_write','conversations_write','ops_write'])assert.deepEqual(by.get(name)?.annotations,{readOnlyHint:false,destructiveHint:true,openWorldHint:false});
 assert.deepEqual(by.get('intake_show')?.inputSchema.properties.query.properties.view.enum,['history','links','context']);
 assert.deepEqual(by.get('sessions_list')?.inputSchema.properties.query.properties.view.enum,['lifecycle','tree']);
 for(const name of DEFAULT_TOOL_EXCLUSIONS){assert(!by.has(name));assert(!DOMAIN_TOOL_NAMES.some(domain=>by.get(domain)?.inputSchema.properties.action.enum.includes(name)));}
 assert.deepEqual(legacyTools().map(t=>t.name),[...LEGACY_MCP_TOOL_NAMES]);assert.equal(LEGACY_MCP_TOOL_NAMES.length,87);
 assert(Buffer.byteLength(JSON.stringify({tools:legacyTools()}))<=45000);
 assert(Buffer.byteLength(JSON.stringify({jsonrpc:'2.0',id:1,result:{tools:legacyTools()}}))<=45000);
 assert.equal(Object.keys(LEGACY_MCP_ALIASES).length,87);
 assert.deepEqual(new Set([...Object.keys(LEGACY_MCP_ALIASES),...Object.keys(EXTENDED_MCP_ALIASES)]),new Set(MCP_ROUTES.map(r=>r.name)));
});
test('fixed legacy launch contract keeps interactive/headless mode semantics',async()=>{
 assert.equal((await toolRequest('http://example.com','start_session',{payload:{workspace:'x',machine:'m'}},'Bearer x').json()).mode,'interactive');
 assert.equal((await toolRequest('http://example.com','start_agent',{payload:{workspace:'x'}},'Bearer x').json()).mode,'headless');
 assert.throws(()=>toolRequest('http://example.com','start_session',{payload:{mode:'headless'}},'Bearer x'),/invalid_launch_mode/);
});
test('path/query/payload validation cannot choose another origin or route',()=>{
 for(const id of ['..','.','../x','%2e%2e','https://evil.test','x\\y','x?y','x#z'])assert.throws(()=>toolRequest('http://example.com','sessions_show',{id},'Bearer x'));
 assert.throws(()=>toolRequest('http://example.com','fleet',{payload:{}},'Bearer x'));
 assert.throws(()=>toolRequest('http://example.com','fleet',{query:{x:{}}},'Bearer x'));
 const r=toolRequest('http://example.com','claims_release',{id:'project:src'},'Bearer x');
 assert.equal(new URL(r.url).pathname,'/api/bus/claims/project%3Asrc/release');assert.equal(r.headers.get('Cookie'),null);
 assert.equal(new URL(toolRequest('http://example.com','claims_release',{id:'agent-hub:src/mcp.ts'},'Bearer x').url).pathname,'/api/bus/claims/agent-hub%3Asrc%2Fmcp.ts/release');
});
