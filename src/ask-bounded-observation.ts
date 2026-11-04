/** Read-only observations for manual Ask maintenance. Every source is bounded
 * before eligibility filters; callers must refuse unknown input before effects. */
type Sql={exec(query:string,...args:any[]):{toArray():any[]}};
type Row=Record<string,any>;
const LIMIT=128;

export function boundedRecentHoleInput(sql:Sql,since:number):{rows:Row[];limited:boolean} {
  const raw=sql.exec(`SELECT agent_id,seq,role,source FROM ask_messages INDEXED BY ask_messages_received
    WHERE received_at>=? ORDER BY received_at,rowid LIMIT 129`,since).toArray();
  // A partial set could invent a gap between otherwise contiguous refs.
  if(raw.length>LIMIT)return {rows:[],limited:true};
  const unique=new Map<string,Row>();
  for(const row of raw)if(row.role==='user'&&row.source!=='backfill')unique.set(JSON.stringify([row.agent_id,row.seq]),{agent_id:row.agent_id,seq:row.seq});
  return {rows:[...unique.values()].sort((a,b)=>a.seq-b.seq),limited:false};
}

export type AskBatchKind='asks_unmapped'|'asks_dropped'|'asks_unrecorded';
const batchSource=(kind:AskBatchKind)=>kind==='asks_unrecorded'
  ?{table:'ask_gaps',column:'alert_subject',index:'ask_gaps_subject'}
  :{table:'asks',column:kind==='asks_dropped'?'dropped_subject':'alert_subject',index:kind==='asks_dropped'?'asks_dropped_subject':'asks_alert_subject'};

export function boundedBatchSubjects(sql:Sql,kind:AskBatchKind):{subjects:string[];limited:boolean} {
  const {table,column,index}=batchSource(kind);
  // DISTINCT LIMIT would still walk an arbitrarily large first subject.
  const raw=sql.exec(`SELECT ${column} AS subject FROM ${table} INDEXED BY ${index}
    WHERE ${column}>=? AND ${column}<? ORDER BY ${column},rowid LIMIT 129`,kind+'-',kind+'.').toArray();
  return {subjects:[...new Set<string>(raw.slice(0,LIMIT).map(row=>String(row.subject)))],limited:raw.length>LIMIT};
}

export function batchCompleteStillOpen(sql:Sql,kind:AskBatchKind,subject:string):{stillOpen:boolean|null;limited:boolean} {
  const {table,column,index}=batchSource(kind);
  const raw=sql.exec(`SELECT ${kind==='asks_unrecorded'?'resolved_at':'state'}
    FROM ${table} INDEXED BY ${index} WHERE ${column}=? ORDER BY rowid LIMIT 129`,subject).toArray();
  const positive=raw.some(row=>kind==='asks_unrecorded'?row.resolved_at===null
    :kind==='asks_unmapped'?row.state==='unmapped':['dropped_after_done','mapped'].includes(row.state));
  const limited=raw.length>LIMIT;
  return {stillOpen:positive?true:limited?null:false,limited};
}

export function boundedReconcilePage(sql:Sql,after:number,includeBackfill:boolean,maxMessages=100):{
  messages:Row[];asks:Row[];toCursor:number;sourceRowsScanned:number;observationPending:boolean;limited:boolean;
} {
  const source=sql.exec('SELECT rowid AS cursor,* FROM ask_messages WHERE rowid>? ORDER BY rowid LIMIT 129',after).toArray();
  const maximum=Math.min(LIMIT,Math.max(1,Math.floor(maxMessages)||100));
  const messages:Row[]=[];let scanned=0,toCursor=after;
  for(const row of source.slice(0,LIMIT)) {
    scanned++;toCursor=Number(row.cursor);
    if(row.role!=='user'||!includeBackfill&&row.source==='backfill')continue;
    messages.push(row);if(messages.length>=maximum)break;
  }
  const observationPending=source.length>LIMIT||scanned<Math.min(source.length,LIMIT);
  const asks:Row[]=[];let remaining=LIMIT;
  for(const message of messages) {
    const parts=sql.exec('SELECT * FROM asks WHERE message_id=? ORDER BY part LIMIT ?',message.id,remaining+1).toArray();
    if(parts.length>remaining)return {messages:[],asks:[],toCursor:after,sourceRowsScanned:scanned,observationPending:true,limited:true};
    remaining-=parts.length;
    for(const ask of parts)if(ask.state!=='not_an_ask')asks.push({...ask,cursor:message.cursor,agent_id:message.agent_id,
      agent_name:message.agent_name,t_id:message.t_id,ts:message.ts,source:message.source,intake_hint:message.intake_hint,received_at:message.received_at});
  }
  return {messages,asks,toCursor,sourceRowsScanned:scanned,observationPending,limited:false};
}
