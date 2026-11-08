// Regression: the Node adapter must emit each Set-Cookie as its own header.
// Comma-folding them made browsers apply hub2_login's Max-Age=0 to hub2_session.
import assert from 'node:assert/strict';
import http from 'node:http';
import { writeResponse } from '../node/server.ts';

const session = 'hub2_session=abc; Path=/; Max-Age=43200; Secure; HttpOnly; SameSite=Lax';
const login = 'hub2_login=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax';
const server = http.createServer((_req, out) => {
  const headers = new Headers({ Location: '/device' });
  headers.append('Set-Cookie', session);
  headers.append('Set-Cookie', login);
  void writeResponse(new Response(null, { status: 303, headers }), out);
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
try {
  const res = await new Promise((resolve) =>
    http.get({ host: '127.0.0.1', port: server.address().port, path: '/' }, resolve));
  assert.equal(res.statusCode, 303);
  assert.equal(res.headers.location, '/device');
  assert.deepEqual(res.headers['set-cookie'], [session, login]);
  res.resume();
  console.log('node-set-cookie ok');
} finally {
  await new Promise((r) => server.close(r));
}
