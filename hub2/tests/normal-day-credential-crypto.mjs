// Compatibility with the original direct WebCrypto envelope; synthetic values only.
import assert from 'node:assert/strict';
import {credentialKey,sealCredential,openCredential} from '../src/credential-crypto.ts';
const key=await credentialKey('11'.repeat(32)),iv=Uint8Array.from({length:12},(_,i)=>i);
const plain=new TextEncoder().encode('synthetic-compatibility-value');
for(const fields of [['account-do-id','codex',3],['hub2-worker-credential','session-id','operation','machine','intake','project',1,123456]]){
 const aad=new TextEncoder().encode(JSON.stringify(fields));
 const original=await crypto.subtle.encrypt({name:'AES-GCM',iv,additionalData:aad},key,plain);
 assert.deepEqual(await openCredential(key,iv,new Uint8Array(original),aad),plain);
 const sealed=await sealCredential(key,plain,aad);
 assert.equal(sealed.iv.length,12);
 assert.deepEqual(new Uint8Array(await crypto.subtle.decrypt({name:'AES-GCM',iv:sealed.iv,additionalData:aad},key,sealed.data)),plain);
 await assert.rejects(openCredential(key,sealed.iv,sealed.data,new TextEncoder().encode(JSON.stringify([...fields,'wrong-context']))));
}
assert.throws(()=>credentialKey('invalid'));
console.log(JSON.stringify({passed:true,original_account_and_worker_envelopes:true,wrong_context_rejected:true}));
