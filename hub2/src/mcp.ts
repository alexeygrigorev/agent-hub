import { boundedText, error, HttpError } from './http';
import type { Actor, Hub2Env, JsonRecord } from './types';
type Invoke = (r: Request, env: Hub2Env) => Promise<Response>;
// Shared mapping keeps transport validation and mutation bodies consistent.
const definitions = [
  ['repos_list', 'GET', '/api/repos', 'Read canonical registry page.'],
  ['repos_show', 'GET', '/api/repos/:name', 'Read repository metadata.'],
  ['repos_edit', 'PATCH', '/api/repos/:name', 'Edit repository metadata.'],
  ['intake_list', 'GET', '/api/intakes', 'Read authorized intake page.'],
  ['intake_create', 'POST', '/api/intakes', 'Create durable intake with acceptance criteria.'],
  ['intake_show', 'GET', '/api/intakes/:id', 'Read intake.'],
  ['intake_history', 'GET', '/api/intakes/:id/history', 'Read intake event page.'],
  ['intake_start', 'POST', '/api/intakes/:id/start', 'Request admitted native launch.'],
  ['sessions_list', 'GET', '/api/sessions', 'Read session page.'],
  ['sessions_show', 'GET', '/api/sessions/:id', 'Read native session.'],
  ['session_send', 'POST', '/api/sessions/:id/commands', 'Create idempotent native command.'],
  ['status', 'GET', '/api/status', 'Read bounded status.'],
  ['bus_send', 'POST', '/api/messages', 'Send as a currently granted identity.'],
  ['bus_inbox', 'GET', '/api/messages', 'Read mailbox page.'],
  ['bus_status', 'GET', '/api/messages/:id/status', 'Read message delivery stages.'],
  ['bus_show', 'GET', '/api/messages/:id', 'Read message.'],
  ['bus_accept', 'POST', '/api/messages/:id/accept', 'Record epoch-bound acceptance.'],
  ['bus_read', 'POST', '/api/messages/:id/read', 'Record epoch-bound read.'],
  ['bus_ack', 'POST', '/api/messages/:id/ack', 'Record epoch-bound acknowledgement.'],
  ['bus_complete', 'POST', '/api/messages/:id/complete', 'Record epoch-bound completion.'],
  ['claims_create', 'POST', '/api/claims', 'Acquire path claim.'],
  ['claims_release', 'POST', '/api/claims/:id/release', 'Release owned claim.'],
  ['schedules_create', 'POST', '/api/schedules', 'Create recurring schedule.'],
];
const tools = Object.fromEntries(
  definitions.map(([name, method, path, description]) => [
    name,
    {
      method,
      description,
      path: (args: JsonRecord) => path.replace(/:(id|name)/g, (_, key) => required(args, key)),
      ...(method === 'GET'
        ? {}
        : {
            body: (args: JsonRecord) => (name === 'repos_edit' ? args.patch : args),
          }),
    },
  ]),
);
const aliases: Record<string, string> = {
  msg_send: 'bus_send',
  msg_reply: 'bus_send',
  bus_reply: 'bus_send',
  msg_inbox: 'bus_inbox',
  msg_show: 'bus_show',
  msg_ack: 'bus_ack',
  msg_status: 'bus_status',
  msg_accept: 'bus_accept',
  msg_complete: 'bus_complete',
};
for (const [name, target] of Object.entries(aliases))
  if (tools[target]) tools[name] = tools[target];
function required(a: JsonRecord, k: string): string {
  const v = a[k];
  if (typeof v !== 'string' || !v || v.length > 180) throw new HttpError('invalid_' + k, 400);
  return encodeURIComponent(v);
}
export async function mcp(
  request: Request,
  env: Hub2Env,
  invoke: Invoke,
  actor: Actor,
): Promise<Response> {
  if (request.method !== 'POST') return error('method_not_allowed', 405);
  let call: any;
  try {
    call = JSON.parse(await boundedText(request, 32768));
  } catch {
    return Response.json(
      {
        jsonrpc: '2.0',
        id: null,
        error: { code: -32700, message: 'Parse error' },
      },
      { status: 400 },
    );
  }
  if (
    !call ||
    call.jsonrpc !== '2.0' ||
    typeof call.method !== 'string' ||
    !(call.id === undefined || typeof call.id === 'string' || typeof call.id === 'number')
  )
    return Response.json(
      {
        jsonrpc: '2.0',
        id: null,
        error: { code: -32600, message: 'Invalid request' },
      },
      { status: 400 },
    );
  if (call.method.startsWith('notifications/')) return new Response(null, { status: 202 });
  const result = (value: unknown) =>
    Response.json(
      { jsonrpc: '2.0', id: call.id ?? null, result: value },
      { headers: { 'Cache-Control': 'no-store' } },
    );
  if (call.method === 'initialize')
    return result({
      protocolVersion: '2024-11-05',
      capabilities: { tools: {} },
      serverInfo: { name: 'agent-hub2', version: '2' },
    });
  if (call.method === 'ping') return result({});
  if (call.method === 'tools/list')
    return result({
      tools: Object.entries(tools).map(([name, t]) => ({
        name,
        description: t.description,
        inputSchema: {
          type: 'object',
          properties: {},
          additionalProperties: true,
        },
      })),
    });
  if (call.method !== 'tools/call')
    return Response.json({
      jsonrpc: '2.0',
      id: call.id ?? null,
      error: { code: -32601, message: 'Method not found' },
    });
  const name = call.params?.name;
  const tool = tools[name];
  let args = call.params?.arguments;
  if (!tool || !args || typeof args !== 'object' || Array.isArray(args))
    return Response.json({
      jsonrpc: '2.0',
      id: call.id ?? null,
      error: { code: -32602, message: 'Invalid tool or arguments' },
    });
  try {
    args = {
      ...args,
      recipient: args.recipient ?? args.to_actor,
      key: args.key ?? args.idempotency_key,
      epoch: args.epoch ?? args.lease_epoch,
    };
    if (args.from_actor && args.from_actor !== actor.id) {
      if (typeof args.from_actor !== 'string' || !args.from_actor.startsWith('agent:'))
        throw new HttpError('sender_not_authorized', 403);
      if (args.as && args.as !== args.from_actor)
        throw new HttpError('sender_identity_conflict', 400);
      args.as = args.from_actor;
    }
    if (name?.startsWith('msg_') || name?.startsWith('bus_')) {
      if (
        ['kind', 'ref', 'deliver_by_s', 'thread_id'].some((key) => args[key] !== undefined) ||
        (args.delivery && args.delivery !== 'inbox')
      )
        throw new HttpError('unsupported_message_option', 400);
    }
    const requestHeaders = new Headers(request.headers);
    requestHeaders.set('Content-Type', 'application/json');
    if (name === 'msg_reply' || name === 'bus_reply') {
      const parentUrl = new URL('/api/messages/' + required(args, 'reply_to'), request.url);
      if (args.as) parentUrl.searchParams.set('as', String(args.as));
      const parentResponse = await invoke(new Request(parentUrl, { headers: requestHeaders }), env);
      if (!parentResponse.ok)
        return result({
          isError: true,
          content: [{ type: 'text', text: await parentResponse.text() }],
        });
      const parent = (
        (await parentResponse.json()) as {
          message: { sender: string; recipient: string };
        }
      ).message;
      args.parent_sender = parent.sender;
      args.recipient ??= parent.sender === (args.as || actor.id) ? parent.recipient : parent.sender;
    }
    const url = new URL(tool.path(args), request.url);
    if (args.actor !== undefined && args.mailbox === undefined) args.mailbox = args.actor;
    if (
      args.cursor !== undefined &&
      !url.pathname.startsWith('/api/messages') &&
      args.after === undefined
    ) {
      args.after = args.cursor;
      delete args.cursor;
    }
    for (const k of [
      'project',
      'after',
      'cursor',
      'limit',
      'status',
      'mailbox',
      'as',
      'machine_id',
      'q',
    ])
      if (args[k] !== undefined) {
        if (!['string', 'number'].includes(typeof args[k]))
          throw new HttpError('invalid_' + k, 400);
        url.searchParams.set(k, String(args[k]));
      }
    const headers = new Headers(request.headers);
    headers.set('Content-Type', 'application/json');
    const response = await invoke(
      new Request(url, {
        method: tool.method,
        headers,
        ...(tool.body ? { body: JSON.stringify(tool.body(args)) } : {}),
      }),
      env,
    );
    const body = await response.text();
    if (response.ok && ['msg_show', 'bus_show', 'msg_inbox'].includes(name)) {
      const data = JSON.parse(body),
        messages = data.items || (data.message ? [data.message] : []);
      for (const message of messages)
        if (message.recipient === (args.as || actor.id) && !message.read_at) {
          const receiptUrl = new URL(
            '/api/messages/' + encodeURIComponent(message.id) + '/read',
            request.url,
          );
          if (args.as) receiptUrl.searchParams.set('as', String(args.as));
          const receipt = await invoke(
            new Request(receiptUrl, {
              method: 'POST',
              headers: requestHeaders,
              body: JSON.stringify({ epoch: message.epoch }),
            }),
            env,
          );
          if (!receipt.ok)
            return result({
              isError: true,
              content: [{ type: 'text', text: await receipt.text() }],
            });
        }
    }
    return result({
      isError: !response.ok,
      content: [{ type: 'text', text: body }],
      structuredContent: { status: response.status },
    });
  } catch (caught) {
    return result({
      isError: true,
      content: [
        {
          type: 'text',
          text: JSON.stringify({
            error: caught instanceof HttpError ? caught.code : 'invalid_tool_request',
          }),
        },
      ],
    });
  }
}
