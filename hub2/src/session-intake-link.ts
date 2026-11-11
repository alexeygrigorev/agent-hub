import { MeteredDb } from './db';
import { actorFrom, bodyObject, boundedString, error, hasScope, json } from './http';
import type { Hub2Env, JsonRecord } from './types';

type LinkSession = {id:string;project_id:string;intake_id:string;machine_id:string;native_id:string|null};

/** Called only after the machine has resolved its retained canonical session. */
export async function linkSession(request:Request, session:LinkSession|undefined, env:Hub2Env):Promise<Response> {
  const actor = actorFrom(request);
  if (actor?.kind !== 'operator' || !hasScope(actor, 'operator:admin')) return error('forbidden', 403);
  if (!session) return error('session_not_found', 404);
  const body = await bodyObject(request, 4096);
  if (Object.keys(body).some(key => !['key','intake_task_id'].includes(key))
    || typeof body.key !== 'string' || !/^[A-Za-z0-9_.:-]{1,160}$/.test(body.key)
    || typeof body.intake_task_id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:-]{5,127}$/.test(body.intake_task_id)) return error('invalid_intake_link', 400);
  const headers = {'Content-Type':'application/json','x-hub2-actor':JSON.stringify(actor)};
  const control = env.CONTROL.get(env.CONTROL.idFromName('global'));
  const resolved = await control.fetch(new Request('https://control.hub2.internal/api/routes/intake/' + encodeURIComponent(body.intake_task_id), {headers}));
  if (!resolved.ok) return resolved;
  const {route} = await resolved.json() as {route:{id:string;owner:string;resource_id:string}};
  if (route.resource_id !== session.project_id || route.owner !== session.project_id) return error('intake_project_mismatch', 409);
  const project = env.PROJECTS.get(env.PROJECTS.idFromName(route.owner));
  return project.fetch(new Request('https://project.hub2.internal/_internal/session-link', {
    method:'POST', headers, body:JSON.stringify({key:body.key,intake_task_id:route.id,session_id:session.id,
      machine_id:session.machine_id,project_id:session.project_id,primary_intake_id:session.intake_id,native_id:session.native_id}),
  }));
}

/** Secondary work attribution never changes launch, credential or completion bindings. */
export class SessionIntakeLink {
  constructor(private db: MeteredDb,
    private transaction: <T>(callback: () => T) => T,
    private append: (taskId:string,eventKey:string,actorId:string,kind:string,body:JsonRecord,at:number) => void) {}

  links(intakeId: string): JsonRecord[] {
    return this.db.all<{body_json: string}>(
      "SELECT body_json FROM intake_events WHERE task_id=? AND kind='session_linked' ORDER BY seq LIMIT 100",
      intakeId,
    ).map(row => JSON.parse(row.body_json) as JsonRecord);
  }

  async receive(request: Request): Promise<Response> {
    const actor = actorFrom(request);
    if (actor?.kind !== 'operator' || !hasScope(actor, 'operator:admin')) return error('forbidden', 403);
    const body = await bodyObject(request, 4096);
    if (Object.keys(body).some(key => !['key','intake_task_id','session_id','machine_id','project_id','primary_intake_id','native_id'].includes(key))
      || !['key','intake_task_id','session_id','machine_id','project_id','primary_intake_id'].every(key => boundedString(body[key], 180))
      || typeof body.key !== 'string' || !/^[A-Za-z0-9_.:-]{1,160}$/.test(body.key)
      || (body.native_id !== null && !boundedString(body.native_id, 180))) return error('invalid_intake_link', 400);
    return this.transaction(() => {
      const intake = this.db.one<{id:string;project:string;session_id:string|null}>('SELECT id,project,session_id FROM intakes WHERE id=?', body.intake_task_id);
      if (!intake) return error('unknown_intake_task', 409);
      if (intake.project !== body.project_id) return error('intake_project_mismatch', 409);
      const eventKey = 'session-link:' + body.session_id;
      const previous = this.db.one<{task_id:string;body_json:string}>('SELECT task_id,body_json FROM intake_events WHERE event_key=?', eventKey);
      if (previous && previous.task_id !== intake.id) return error('intake_link_conflict', 409);
      if (intake.session_id && intake.session_id !== body.session_id) return error('intake_link_conflict', 409);
      const links = this.links(intake.id);
      if (links.some(link => link.session_id !== body.session_id)) return error('intake_link_conflict', 409);
      const link = previous ? JSON.parse(previous.body_json) as JsonRecord : {
        intake_task_id: intake.id, session_id: body.session_id, machine_id: body.machine_id,
        primary_intake_id: body.primary_intake_id, native_id: body.native_id,
        source: 'manual', linked_by: actor!.id,
      };
      if (!previous) {
        const at = Date.now();
        this.append(intake.id, eventKey, actor!.id, 'session_linked', link, at);
        this.db.run('UPDATE intakes SET updated_at=? WHERE id=?', at, intake.id);
      }
      return json({already_linked: !!previous, link});
    });
  }
}
