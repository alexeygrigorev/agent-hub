/** Deployed (machine_id,state,created_at,implicit rowid) index pagination.
 * Separate equality/range arms avoid sorting a large equal-time prefix. */
type Sql={exec(query:string,...args:any[]):{toArray():any[]}};
export type CredentialSessionCursor={at:string;row:number};
export function credentialSessionCursor(row:any):string {
  return JSON.stringify({at:String(row.created_at),row:Number(row.scan_id)});
}
export function runningCredentialSessionPage(sql:Sql,machine:string,after:string|number=0):any[] {
  let cursor:CredentialSessionCursor={at:'',row:0};
  if(after && after!=='0') {
    if(typeof after==='number'||/^\d+$/.test(after)) {
      // Legacy cursors ordered global rowid; restart once rather than skip
      // rows when switching to the per-host created_at ordering.
    }else{
      const decoded=JSON.parse(after);
      if(!decoded||typeof decoded.at!=='string'||decoded.at.length>128||!Number.isSafeInteger(decoded.row)||decoded.row<0)throw new Error('credential_scan_cursor_invalid');
      cursor=decoded;
    }
  }
  const columns='rowid AS scan_id,id,machine_id,binding_json,fleet_launch_id,created_at';
  const rows=sql.exec(`SELECT ${columns} FROM session_control_sessions INDEXED BY session_control_machine_state
    WHERE machine_id=? AND state='running' AND created_at=? AND rowid>? ORDER BY rowid LIMIT 65`,machine,cursor.at,cursor.row).toArray();
  if(rows.length<65)rows.push(...sql.exec(`SELECT ${columns} FROM session_control_sessions INDEXED BY session_control_machine_state
    WHERE machine_id=? AND state='running' AND created_at>? ORDER BY created_at,rowid LIMIT ?`,machine,cursor.at,65-rows.length).toArray());
  return rows;
}
