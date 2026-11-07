// Server-side half of WebSocketPair, plus the bridge to a real `ws` socket.
// The DO code uses the hibernation-style surface: accept(), send(), close(),
// serializeAttachment/deserializeAttachment and readyState; delivery events
// (webSocketMessage/webSocketClose/webSocketError) come through the runtime
// via the __deliver hooks installed by ctx.acceptWebSocket.

export type WireEndpoint = {
  send(data: string | Uint8Array): void;
  close(code?: number, reason?: string): void;
};

export type SocketDelivery = {
  message(data: string | ArrayBuffer): void;
  closed(): void;
  error(error: unknown): void;
};

type QueuedFrame = { kind: 'send'; data: string } | { kind: 'close'; code?: number; reason?: string };

export class ServerSideSocket {
  readyState = 0; // 0 connecting, 1 open, 2 closing, 3 closed
  /** Installed by ctx.acceptWebSocket; the wire bridge delivers through it. */
  __deliver: SocketDelivery | undefined;
  private wire: WireEndpoint | undefined;
  private outbox: QueuedFrame[] | undefined = [];
  private attachment: unknown;
  private attachmentSet = false;
  readonly tags: string[] = [];

  accept(): void {
    if (this.readyState === 0) this.readyState = 1;
  }

  /** Attaches the live transport once the 101 handshake completes; flushes
   * frames the DO queued between handler return and handshake. */
  bindWire(wire: WireEndpoint): void {
    this.wire = wire;
    const queued = this.outbox;
    this.outbox = undefined;
    this.readyState = 1;
    if (queued) {
      for (const frame of queued) {
        if (frame.kind === 'send') wire.send(frame.data);
        else wire.close(frame.code, frame.reason);
      }
    }
  }

  send(data: string): void {
    if (this.readyState === 3) throw new Error('socket_closed');
    if (this.wire) this.wire.send(data);
    else this.outbox!.push({ kind: 'send', data });
  }

  close(code = 1000, reason = ''): void {
    if (this.readyState === 3) return;
    this.readyState = 2;
    if (this.wire) this.wire.close(code, reason);
    else this.outbox!.push({ kind: 'close', code, reason });
  }

  serializeAttachment(value: unknown): void {
    this.attachment = value;
    this.attachmentSet = true;
  }

  deserializeAttachment(): unknown {
    if (!this.attachmentSet) return undefined;
    return this.attachment;
  }

  /** Wire-side close completed (self- or peer-initiated). */
  wireClosed(): void {
    this.readyState = 3;
    this.wire = undefined;
    this.outbox = undefined;
    this.__deliver?.closed();
  }
}

export class WebSocketPair {
  /** Workers exposes {client, server}; hub2 destructures via Object.values,
   * so insertion order (client first) is part of the contract. */
  client: { _server: ServerSideSocket };
  server: ServerSideSocket;

  constructor() {
    const server = new ServerSideSocket();
    this.server = server;
    this.client = { _server: server };
  }
}

/** Delivers a wire frame to the DO handler, mirroring the Workers runtime:
 * text frames as string, binary frames as ArrayBuffer. */
export function decodeFrame(data: Buffer): string | ArrayBuffer {
  try {
    return new TextDecoder('utf-8', { fatal: true } as never).decode(data);
  } catch {
    const copy = new ArrayBuffer(data.byteLength);
    new Uint8Array(copy).set(data);
    return copy;
  }
}
