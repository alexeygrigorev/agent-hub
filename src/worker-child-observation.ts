/** Observe child admission from bounded deployed index prefixes. A lower bound
 * may prove a limit; an incomplete absence can never authorize another child. */
type Sql={exec(query:string,...args:any[]):{toArray():any[]}};
export function workerChildObservation(sql:Sql,parent:string,intake:string,now:number) {
  const active="state IN ('queued','held','offered','starting','running','uncertain')";
  const terminal=new Set(['exited','stopped','failed','cancelled','dead','forgotten','pruned']);
  const live=sql.exec(`SELECT parent_id FROM session_control_sessions INDEXED BY session_control_live_created
    WHERE ${active} ORDER BY created_at DESC,id DESC LIMIT 129`).toArray();
  const linked=sql.exec(`SELECT parent_id,state FROM session_control_sessions INDEXED BY intake_queue_session_task
    WHERE json_extract(CASE WHEN json_valid(request_json) THEN request_json ELSE '{}' END,'$.intake_task_id')=?
    ORDER BY rowid LIMIT 129`,intake).toArray();
  const recent=sql.exec(`SELECT parent_id FROM session_control_sessions INDEXED BY session_control_created
    WHERE created_at>? ORDER BY created_at DESC,id DESC LIMIT 129`,new Date(now-3600000).toISOString()).toArray();
  return {
    concurrent:{count:live.filter(row=>row.parent_id===parent).length,limited:live.length>128},
    per_intake:{count:linked.filter(row=>row.parent_id!==null&&!terminal.has(row.state)).length,limited:linked.length>128},
    per_hour:{count:recent.filter(row=>row.parent_id===parent).length,limited:recent.length>128},
  };
}
