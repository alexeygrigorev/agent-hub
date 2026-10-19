// Verify the reviewed kit bytes and family policy, not a duplicated component implementation.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {resolve} from 'node:path';
import {checkBorders} from '../public/vendor/dakit/check-borders.mjs';
import {checkColors} from '../public/vendor/dakit/check-colors.mjs';
import {checkScale} from '../public/vendor/dakit/check-scale.mjs';
const root=resolve(import.meta.dirname,'..'),vendor=resolve(root,'public/vendor/dakit');
const lock=JSON.parse(await readFile(resolve(vendor,'dakit.lock.json'),'utf8'));
assert.equal(lock.version,'0.2.0');assert.equal(lock.commit,'0e283cd');
for(const [path,hash] of Object.entries(lock.files))assert.equal(createHash('sha256').update(await readFile(resolve(vendor,path))).digest('hex'),hash,'Reviewed kit drift: '+path);
const paths=[
  'public/index.html',
  'public/fleet.css','public/fleet.html','public/fleet.js',
  'public/metrics.css','public/metrics.html','public/metrics.js',
  'public/sessions.css','public/sessions.html','public/sessions.js',
  'public/auth.css','public/auth.js'
].map(p=>resolve(root,p));
assert.deepEqual(checkColors(paths),[]);assert.deepEqual(checkBorders(paths),[]);assert.deepEqual(checkScale(paths).failures,[]);
console.log('PASS reviewed official dakit lock (21 files), semantic colors, neutral borders and type/space scales');
