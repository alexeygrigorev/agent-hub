import {test} from 'node:test';import assert from 'node:assert/strict';
import {validateDependencyGraph,eligibleIntakes,planAutoQueue,evaluateAcceptance,auditDone,
  AUTO_POLICY_DEFAULTS,type AutoIntake,type AutoSession,type AutoCapacity} from '../src/auto-policy';

const NOW=1793000000000,MIN=60_000;
const cap=(over:Partial<AutoCapacity>={}):AutoCapacity=>({maxInflight:2,maxPerProject:1,runningTotal:0,runningByProject:{},...over});
const intake=(over:Partial<AutoIntake>):AutoIntake=>({id:over.id??'i',project:'agent-hub',priority:2,status:'open',
  blockedBy:[],sessionId:null,externalLive:false,attempts:0,createdAt:NOW,updatedAt:NOW,...over});
const session=(over:Partial<AutoSession>):AutoSession=>({id:'s',intakeId:null,lastProgressAt:NOW,nudges:[],...over});
const starts=(p:ReturnType<typeof planAutoQueue>)=>p.actions.filter(a=>a.kind==='start') as Extract<ReturnType<typeof planAutoQueue>['actions'][number],{kind:'start'}>[];

// 8edc8f7c: DAG — cycle rejection, dependency eligibility, auto-resume, shared-prereq dedup.
test('dependency graph rejects cycles with the members named and blocks intake on unfinished prereqs',()=>{
 const a=intake({id:'a',blockedBy:['b']}),b=intake({id:'b',blockedBy:['a']}),c=intake({id:'c',blockedBy:['c']});
 const v=validateDependencyGraph([a,b,c]);
 assert.deepEqual(v.cycles.sort(),[['a','b'],['c']]);
 assert.ok(v.errors.some(e=>e.includes('a')&&e.includes('b')),'cycle error names the members');
 const good=validateDependencyGraph([intake({id:'a',blockedBy:['b']}),intake({id:'b'})]);
 assert.deepEqual(good.cycles,[]);assert.deepEqual(good.errors,[]);
 const e=eligibleIntakes([intake({id:'a',blockedBy:['b']}),intake({id:'b',status:'open'})]);
 assert.deepEqual(e.eligible.map(i=>i.id),['b']);
 assert.deepEqual(e.blocked,[{id:'a',reason:'prereq_not_accepted'}]);
});

test('prereq completion resumes waiting parent; shared blocker dedups into one prerequisite at parent priority or higher',()=>{
 const parent=intake({id:'p1',status:'waiting_on_prereq',blockedBy:['pr1'],blockerKey:'reviewer_auth_expired',priority:2});
 const twin=intake({id:'p2',status:'waiting_on_prereq',blockedBy:['pr1'],blockerKey:'reviewer_auth_expired',priority:1});
 const fresh=planAutoQueue({now:NOW,capacity:cap(),intakes:[parent,twin],sessions:[],lastActivityAt:NOW});
 const prereq=fresh.actions.find(a=>a.kind==='create_prereq');
 assert.equal(prereq&&('parent_ids'in prereq?prereq.parent_ids.length:0),2,'same blocker key creates one shared prerequisite');
 assert.equal(prereq&&('blocker_key'in prereq?prereq.blocker_key:''),'reviewer_auth_expired');
 assert.equal(prereq&&('priority'in prereq?prereq.priority:9),1,'prerequisite priority is the highest (lowest number) parent priority');
 const resumed=planAutoQueue({now:NOW,capacity:cap(),intakes:[{...parent},{...twin,blockedBy:['pr2'],blockerKey:'other'},
   intake({id:'pr1',status:'accepted'})],sessions:[],lastActivityAt:NOW});
 assert.deepEqual(resumed.actions.filter(a=>a.kind==='resume').map(a=>('intake_id'in a?a.intake_id:'')),['p1']);
 assert.equal(starts(resumed).find(s=>s.intake_id==='p1')?.reason,'prereqs_satisfied');
 assert.ok(!resumed.actions.some(a=>a.kind==='resume'&&('intake_id'in a&&a.intake_id==='p2')),'still-unsatisfied twin stays waiting');
});

// f0b21a52: native exit is not acceptance; per-AC marks, verified shas, review close the intake.
test('native exit alone never accepts; acceptance needs every AC met with evidence, commits on main, green deploy and review',()=>{
 const exited={acceptanceCriteriaCount:2,acMarks:[{index:0,met:true,evidence:'tests green'},{index:1,met:true,evidence:'dashboard'}],
   commits:['abc123'],commitsVerifiedOnMain:[],deployGreenCommit:null,reviewApproved:false,nativeExitConfirmed:true,exitCode:0};
 const refused=evaluateAcceptance(exited);
 assert.equal(refused.decision,'refuse');
 assert.deepEqual(refused.unverifiedCommits,['abc123']);
 assert.ok(refused.refusals.includes('deploy_not_green'));
 assert.ok(refused.refusals.includes('review_not_approved'));
 assert.ok(refused.refusals.includes('unverified_commits'));
 const unmarked=evaluateAcceptance({...exited,acMarks:[{index:0,met:true,evidence:'tests green'}]});
 assert.ok(unmarked.refusals.includes('ac_marks_missing'));assert.deepEqual(unmarked.unmetAc,[1]);
 const unevidenced=evaluateAcceptance({...exited,acMarks:[{index:0,met:true,evidence:'tests green'},{index:1,met:false,evidence:''}]});
 assert.ok(unevidenced.refusals.includes('ac_evidence_missing'));
 const noEvidence=evaluateAcceptance({...exited,nativeExitConfirmed:false,exitCode:undefined,acMarks:[],commits:[]});
 assert.ok(noEvidence.refusals.includes('no_evidence'),'absent evidence stays unknown, never accepted');
 const accepted=evaluateAcceptance({...exited,commitsVerifiedOnMain:['abc123'],deployGreenCommit:'abc123',reviewApproved:true});
 assert.equal(accepted.decision,'accept');assert.deepEqual(accepted.refusals,[]);
});

test('close gate names unmerged commits and done audit reopens shas that left main',()=>{
 const done=intake({id:'d1',status:'completed-awaiting-review',finalEvidence:{commits:['aa11','bb22']}});
 const gate=evaluateAcceptance({acceptanceCriteriaCount:1,acMarks:[{index:0,met:true,evidence:'ok'}],commits:['aa11','bb22'],
   commitsVerifiedOnMain:['aa11'],deployGreenCommit:'aa11',reviewApproved:true,nativeExitConfirmed:true,exitCode:0});
 assert.deepEqual(gate.unverifiedCommits,['bb22'],'unmerged commit sha is named in the refusal');
 assert.equal(gate.missingDeploy,null);
 const audit=auditDone([done,intake({id:'d2',status:'accepted',finalEvidence:{commits:['aa11']}})],(sha=>sha==='aa11'));
 assert.deepEqual(audit.reopen.map(r=>r.intake_id),['d1']);
 assert.deepEqual(audit.reopen[0].missing_commits,['bb22']);
 assert.ok(!audit.reopen.some(r=>r.intake_id==='d2'),'accepted intake with all commits on main is not reopened');
});

// 2e00e01f: unmerged work watcher requeues after 60 idle minutes; blocked finish-merge raises a prerequisite.
test('unmerged completed work idles 60 minutes then requeues a finish-merge pass; exhausted attempts raise a blocker prerequisite',()=>{
 const unmerged=()=>intake({id:'u1',status:'completed-awaiting-review',updatedAt:NOW,unmergedCommits:['dd44']});
 const watch=planAutoQueue({now:NOW+59*MIN,capacity:cap(),intakes:[unmerged()],sessions:[],lastActivityAt:NOW+59*MIN});
 assert.ok(!watch.actions.some(a=>a.kind==='requeue'),'before 60 idle minutes nothing is requeued');
 assert.equal(watch.nextDeadlineAt,NOW+60*MIN);
 const due=planAutoQueue({now:NOW+61*MIN,capacity:cap(),intakes:[unmerged()],sessions:[],lastActivityAt:NOW+61*MIN});
 const requeue=due.actions.find(a=>a.kind==='requeue');
 assert.equal(requeue&&('reason'in requeue?requeue.reason:''),'unmerged_requeue');
 assert.equal(requeue&&('mode'in requeue?requeue.mode:''),'finish-merge');
 const exhausted=planAutoQueue({now:NOW+61*MIN,capacity:cap(),intakes:[intake({id:'u2',status:'completed-awaiting-review',updatedAt:NOW,
   attempts:AUTO_POLICY_DEFAULTS.maxAttempts,unmergedCommits:['ee55']})],sessions:[],lastActivityAt:NOW+61*MIN});
 assert.ok(!exhausted.actions.some(a=>a.kind==='requeue'));
 const prereq=exhausted.actions.find(a=>a.kind==='create_prereq');
 assert.equal(prereq&&('blocker_key'in prereq?prereq.blocker_key:''),'merge_blocked');
 assert.ok(prereq&&('evidence'in prereq?prereq.evidence.includes('ee55'):false),'prerequisite carries the blocker evidence');
});

// 500fd60b + 27505f04: 30-minute liveness bound, one nudge per 60 min, replace after two unacked nudges, false-stall exemption.
test('session with no progress 30 minutes on an open intake is idle_with_open_ac, nudged once per hour, replaced after two unacked nudges',()=>{
 const linked=session({id:'s1',intakeId:'w1',lastProgressAt:NOW-29*MIN});
 const w1=(at:number)=>intake({id:'w1',status:'running',sessionId:'s1',updatedAt:at});
 const calm=planAutoQueue({now:NOW,capacity:cap({runningTotal:1,runningByProject:{'agent-hub':1}}),
   intakes:[w1(NOW)],sessions:[linked],lastActivityAt:NOW});
 assert.equal(calm.classifications.find(c=>c.session_id==='s1')?.state,'active');
 const idleAt=planAutoQueue({now:NOW+31*MIN,capacity:cap({runningTotal:1,runningByProject:{'agent-hub':1}}),
   intakes:[w1(NOW)],sessions:[{...linked}],lastActivityAt:NOW});
 assert.equal(idleAt.classifications.find(c=>c.session_id==='s1')?.state,'idle_with_open_ac');
 const nudge=idleAt.actions.find(a=>a.kind==='nudge');
 assert.ok(nudge,'first idle crossing sends the resume nudge');
 assert.ok(Array.isArray(nudge&&'open_ac'in nudge?nudge.open_ac:0),'nudge names the open acceptance criteria');
 const nudged=session({id:'s2',intakeId:'w2',lastProgressAt:NOW-40*MIN,nudges:[{at:NOW-10*MIN,acked:false}]});
 const rateLimited=planAutoQueue({now:NOW,capacity:cap({runningTotal:1,runningByProject:{'agent-hub':1}}),
   intakes:[intake({id:'w2',status:'running',sessionId:'s2'})],sessions:[nudged],lastActivityAt:NOW});
 assert.ok(!rateLimited.actions.some(a=>a.kind==='nudge'),'at most one nudge per 60 minutes');
 assert.equal(rateLimited.nextDeadlineAt,NOW+(60-10)*MIN,'next nudge deadline is scheduled');
 const twice=session({id:'s3',intakeId:'w3',lastProgressAt:NOW-95*MIN,nudges:[{at:NOW-90*MIN,acked:false},{at:NOW-35*MIN,acked:false}]});
 const replace=planAutoQueue({now:NOW,capacity:cap({runningTotal:1,runningByProject:{'agent-hub':1}}),
   intakes:[intake({id:'w3',status:'running',sessionId:'s3'})],sessions:[twice],lastActivityAt:NOW});
 const recover=replace.actions.find(a=>a.kind==='recover');
 assert.equal(recover&&('reason'in recover?recover.reason:''),'nudges_unanswered');
 assert.equal(recover&&('intake_id'in recover?recover.intake_id:''),'w3');
 assert.ok(!replace.actions.some(a=>a.kind==='nudge'));
 assert.ok(!replace.classifications.some(c=>c.state==='reaped'),'idle sessions are never silently reaped');
});

test('a long in-flight tool call inside its declared window is not a stall; past the window it is',()=>{
 const polling=session({id:'s4',intakeId:'w4',lastProgressAt:NOW-40*MIN,inFlightTool:{startedAt:NOW-39*MIN,maxMs:45*MIN}});
 const exempt=planAutoQueue({now:NOW,capacity:cap({runningTotal:1,runningByProject:{'agent-hub':1}}),
   intakes:[intake({id:'w4',status:'running',sessionId:'s4'})],sessions:[polling],lastActivityAt:NOW});
 assert.equal(exempt.classifications.find(c=>c.session_id==='s4')?.state,'active');
 assert.ok(!exempt.actions.some(a=>a.kind==='nudge'));
 assert.equal(exempt.nextDeadlineAt,NOW+6*MIN,'the tool-call window bound schedules a re-check');
 const overdue=planAutoQueue({now:NOW+7*MIN,capacity:cap({runningTotal:1,runningByProject:{'agent-hub':1}}),
   intakes:[intake({id:'w4',status:'running',sessionId:'s4'})],sessions:[{...polling}],lastActivityAt:NOW});
 assert.equal(overdue.classifications.find(c=>c.session_id==='s4')?.state,'idle_with_open_ac');
});

// 500fd60b AC4 + 6b53e801: zero sessions with open backlog for 30 minutes kicks the queue and records why idle.
test('backlog with zero working sessions kicks capacity filling and records the idle reason when it cannot start',()=>{
 const kick=planAutoQueue({now:NOW+31*MIN,capacity:cap(),intakes:[intake({id:'k1',createdAt:NOW,updatedAt:NOW-31*MIN})],
   sessions:[],lastActivityAt:NOW-31*MIN});
 assert.equal(starts(kick)[0]?.intake_id,'k1');
 assert.ok(kick.actions.some(a=>a.kind==='alert'&&('reason'in a?a.reason:'')==='idle_backlog_kick'),'kick records why the queue was idle');
 const full=planAutoQueue({now:NOW+31*MIN,capacity:cap({runningTotal:2,runningByProject:{'agent-hub':2}}),
   intakes:[intake({id:'k2'})],sessions:[],lastActivityAt:NOW-31*MIN});
 assert.equal(full.idle?.reason,'capacity_full');
 assert.equal(starts(full).length,0);
 const blocked=planAutoQueue({now:NOW+31*MIN,capacity:cap(),intakes:[intake({id:'k3',blockedBy:['gone']})],sessions:[],lastActivityAt:NOW-31*MIN});
 assert.equal(blocked.idle?.reason,'no_eligible_work');
 assert.equal(starts(blocked).length,0);
 const quiet=planAutoQueue({now:NOW,capacity:cap(),intakes:[],sessions:[],lastActivityAt:NOW});
 assert.equal(quiet.idle,null,'no backlog means no idle explanation');
});

// 85318207 R3 + 0841a966: overdue queued restarts execute, bounded; qualified failures requeue, attempts capped.
test('overdue restarts requeue in due order and unbounded-failure intakes stop at the attempt cap',()=>{
 const overdue=planAutoQueue({now:NOW,capacity:cap(),intakes:[
   intake({id:'o2',status:'failed',restartQueuedAt:NOW-2*MIN,attempts:1,qualifiedFailure:true}),
   intake({id:'o1',status:'failed',restartQueuedAt:NOW-5*MIN,attempts:2,qualifiedFailure:true})],
   sessions:[],lastActivityAt:NOW});
 assert.deepEqual(overdue.actions.filter(a=>a.kind==='requeue').map(a=>('intake_id'in a?a.intake_id:'')),['o1','o2']);
 assert.ok(overdue.actions.filter(a=>a.kind==='requeue').every(a=>('reason'in a?a.reason:'')==='overdue_restart'));
 const capped=planAutoQueue({now:NOW,capacity:cap(),intakes:[intake({id:'x',status:'failed',restartQueuedAt:NOW-1*MIN,
   attempts:AUTO_POLICY_DEFAULTS.maxAttempts,qualifiedFailure:true})],sessions:[],lastActivityAt:NOW});
 assert.ok(!capped.actions.some(a=>a.kind==='requeue'));
 assert.ok(capped.actions.some(a=>a.kind==='alert'&&('reason'in a?a.reason:'')==='attempt_cap_reached'),'hitting the cap alerts instead of looping');
});

// 85318207 R1 + efcfa907 + 68145244 + 51e493de: real-project drain, priority/newest-first/promote ordering, per-project cap, live-link dedup.
test('queue fills every project up to global and per-project caps in priority, promote, newest-first order without duplicating live sessions',()=>{
 const many=[0,1,2,3].map(n=>intake({id:`n${n}`,project:n===3?'writer':'agent-hub',priority:(n%2)+1,createdAt:NOW-n*MIN}));
 const fill=planAutoQueue({now:NOW,capacity:cap({maxInflight:3,maxPerProject:2}),intakes:many,sessions:[],lastActivityAt:NOW});
 assert.deepEqual(starts(fill).map(s=>s.intake_id),['n0','n2','n3'],'high priority first, newest first; per-project cap reroutes to the second project');
 assert.equal(fill.capacity_after.runningTotal,3);
 const promoted=planAutoQueue({now:NOW,capacity:cap({maxInflight:1}),intakes:[intake({id:'old',createdAt:NOW-9*MIN,priority:1}),
   intake({id:'promoted',createdAt:NOW-8*MIN,priority:1,promotedAt:NOW-1*MIN})],sessions:[],lastActivityAt:NOW});
 assert.deepEqual(starts(promoted).map(s=>s.intake_id),['promoted'],'promote-to-front beats even high priority');
 const live=planAutoQueue({now:NOW,capacity:cap({runningTotal:1,runningByProject:{'agent-hub':1}}),
   intakes:[intake({id:'busy',status:'running',sessionId:'s9'})],sessions:[session({id:'s9',intakeId:'busy',lastProgressAt:NOW})],lastActivityAt:NOW});
 assert.equal(starts(live).length,0,'running intake with a live session is never started twice');
 const block=eligibleIntakes([intake({id:'ext',externalLive:true})]).blocked;
 assert.deepEqual(block,[{id:'ext',reason:'linked_live_external_session'}],'a linked live external session is never duplicated');
});
