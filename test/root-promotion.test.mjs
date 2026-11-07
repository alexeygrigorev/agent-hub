import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import test from 'node:test';
const read = path => readFileSync(new URL('../' + path, import.meta.url), 'utf8');
const config = path => JSON.parse(read(path).split('\n').filter(line => !line.trim().startsWith('//')).join('\n'));
test('canonical root configuration selects six-DO Hub2 only', () => {
 const root=config('wrangler.jsonc'), nested=config('hub2/wrangler.production.jsonc');
 assert.equal(root.main,'hub2/production-worker.ts');
 assert.equal(root.assets.directory,'hub2/public');
 assert.equal(root.workers_dev,true,'preserve the approved workers.dev endpoint across deployments');
 assert.equal(Object.hasOwn(root,'route') || Object.hasOwn(root,'routes'),false,'do not replace the dashboard-managed route');
 const normalized=structuredClone(root);
 normalized.main='production-worker.ts'; normalized.assets.directory='./public';
 normalized.$schema='../node_modules/wrangler/config-schema.json';
 assert.deepEqual(normalized,nested);
 assert.deepEqual(root.durable_objects.bindings.map(x=>x.name).sort(),['ACCOUNTS','BUDGET','CONTROL','MACHINES','MAILBOXES','PROJECTS']);
 assert.equal(root.migrations,undefined); assert.equal(root.exports.Hub.state,'deleted');
 for(const binding of root.durable_objects.bindings) {
  assert.equal(binding.script_name,undefined);
  assert.equal(root.exports[binding.class_name].storage,'sqlite');
 }
});
test('root release is manually gated behind full Hub2 and distribution checks',()=>{
 const pkg=JSON.parse(read('package.json')); assert.equal(pkg.version,'2.0.0');
 assert.match(pkg.scripts.build,/--config wrangler.jsonc --dry-run/);
 const flow=read('.github/workflows/deploy.yml');
 assert.match(flow,/uses: \.\/\.github\/workflows\/hub2.yml/);
 assert.match(flow,/github.event_name == 'workflow_dispatch'/);
 assert.match(flow,/needs: \[hub2, distribution\]/);
 assert.doesNotMatch(flow,/DataTalksClub\/auth|build-hub-cli|host\/tests|playwright/);
 assert.match(flow,/release_mode:[\s\S]*default: bootstrap/);
 assert.match(flow,/render-wrangler-deploy-config\.py/);
 assert.match(flow,/wrangler deploy --config wrangler\.deploy\.jsonc/);
 assert.doesNotMatch(read('.github/workflows/rollback.yml'),/versions deploy/);
});

test('legacy rollback helper cannot deploy a historical Worker version',()=>{
 const helper=read('scripts/ci/rollback.py');
 assert.doesNotMatch(helper,/subprocess|wrangler.*versions.*deploy|versions.*deploy/);
 const python=process.platform==='win32'?'python':'python3';
 const root=fileURLToPath(new URL('..',import.meta.url));
 const result=spawnSync(python,['scripts/ci/rollback.py','--version-id','hub1-version','--yes'],{
  cwd:root,encoding:'utf8',timeout:5000,
 });
 assert.equal(result.error,undefined,result.error?.message);
 assert.equal(result.status,2,result.stdout+'\n'+result.stderr);
 assert.match(result.stderr,/legacy Cloudflare version operations are disabled/);
});
