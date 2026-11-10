import { test } from 'node:test';
import assert from 'node:assert/strict';
import { inactiveReason, isLiveActiveSession, LIVE_SNAPSHOT_TTL_MS, MAX_ACTIVE_SESSIONS, SESSION_IDLE_MS } from '../src/session-liveness.ts';

const now=Date.now();
const live={session_id:'s1',native_id:'native-1',phase:'running',last_activity_at:now-1000,observed_at:now};

test('only fresh native panes doing recent work count as active',()=>{
 assert.equal(MAX_ACTIVE_SESSIONS,50);
 assert.equal(isLiveActiveSession(live,now),true);
 assert.equal(isLiveActiveSession({...live,native_id:null},now),false);
 assert.equal(isLiveActiveSession({...live,phase:'exited'},now),false);
 assert.equal(isLiveActiveSession({...live,phase:'missing'},now),false);
 assert.equal(isLiveActiveSession({...live,containment_empty:true},now),false);
 assert.equal(isLiveActiveSession({...live,observed_at:now-LIVE_SNAPSHOT_TTL_MS-1},now),false);
 assert.equal(isLiveActiveSession({...live,last_activity_at:now-SESSION_IDLE_MS-1},now),false);
 assert.equal(isLiveActiveSession({...live,last_activity_at:null},now),false);
});

test('the reaper labels exited, missing, and idle panes without counting DB state',()=>{
 assert.equal(inactiveReason(live,now),null);
 assert.equal(inactiveReason({...live,phase:'exited'},now),'dead');
 assert.equal(inactiveReason({...live,phase:'missing'},now),'dead');
 assert.equal(inactiveReason({...live,last_activity_at:now-SESSION_IDLE_MS-1},now),'idle');
 assert.equal(inactiveReason({...live,native_id:null},now),'dead');
});
