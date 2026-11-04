// Isolated dev-only entry point: production Worker and actual SQLite DO classes.
import worker from '../../src/worker';
import type { Hub2Env } from '../../src/types';
import { ControlDO as Control } from '../../src/control-do';
import { ProjectDO as Project } from '../../src/project-do';
import { MachineDO as Machine } from '../../src/machine-do';
import { AccountDO as Account } from '../../src/account-do';
export { MailboxDO } from '../../src/mailbox-do';
async function measured(request:Request,ctx:DurableObjectState,fetcher:()=>Promise<Response>){
  const response=await fetcher();
  return new URL(request.url).pathname==='/_metrics'
    ? Response.json({...await response.json() as object,alarm:await ctx.storage.getAlarm()}) : response;
}
export class ControlDO extends Control {
  constructor(private fixture:DurableObjectState,env:Hub2Env){super(fixture,env);}
  fetch(r:Request){return measured(r,this.fixture,()=>super.fetch(r));}
}
export class ProjectDO extends Project {
  constructor(private fixture:DurableObjectState,env:Hub2Env){super(fixture,env);}
  fetch(r:Request){return measured(r,this.fixture,()=>super.fetch(r));}
}
export class MachineDO extends Machine {
  constructor(private fixture:DurableObjectState,env:Hub2Env){super(fixture,env);}
  fetch(r:Request){return measured(r,this.fixture,()=>super.fetch(r));}
}
export class AccountDO extends Account {
  constructor(private fixture:DurableObjectState,env:Hub2Env){super(fixture,env);}
  fetch(r:Request){return measured(r,this.fixture,()=>super.fetch(r));}
}

export default { async fetch(request: Request, env: Hub2Env): Promise<Response> {
  const url = new URL(request.url);
  if (!['/e2e/metrics','/e2e/status'].includes(url.pathname)) return worker.fetch(request, env);
  if (!env.HUB2_DEV_OPERATOR_TOKEN || request.headers.get('Authorization') !== `Bearer ${env.HUB2_DEV_OPERATOR_TOKEN}`) {
    return new Response('unauthorized', { status: 401 });
  }
  const namespaces = { control: env.CONTROL, project: env.PROJECTS, machine: env.MACHINES, account: env.ACCOUNTS, mailbox: env.MAILBOXES };
  const kind = url.searchParams.get('kind') as keyof typeof namespaces;
  const id = url.searchParams.get('id');
  if (!namespaces[kind] || !id || id.length > 180) return new Response('invalid metric target', { status: 400 });
  if (url.pathname==='/e2e/status' && kind!=='machine') return new Response('invalid status kind',{status:400});
  return namespaces[kind].get(namespaces[kind].idFromName(id)).fetch('https://metric.internal'+(url.pathname==='/e2e/status'?'/_internal/status':'/_metrics'));
}};
