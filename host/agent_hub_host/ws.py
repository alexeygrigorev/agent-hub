import struct
import socket

class WSConnection:
    def __init__(self, sock, rest: bytes, rng):
        self.sock = sock
        self.rest = rest
        self.rng = rng
        self.state = "HEADER"
        self.fin = False
        self.opcode = 0
        self.has_mask = False
        self.length = 0
        self.mask = None
        self.payload = bytearray()
        
    def send_frame(self, opcode: int, payload: bytes):
        mask = self.rng.randbytes(4)
        masked_payload = bytearray(len(payload))
        for i in range(len(payload)):
            masked_payload[i] = payload[i] ^ mask[i % 4]
            
        header = bytearray([0x80 | opcode])
        if len(payload) < 126:
            header.append(0x80 | len(payload))
        elif len(payload) < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", len(payload)))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", len(payload)))
            
        header.extend(mask)
        self.sock.sendall(header + masked_payload)

    def recv_frame(self):
        while True:
            if self.state == "HEADER":
                if len(self.rest) < 2:
                    chunk = self.sock.recv(4096)
                    if not chunk: raise ConnectionError("Closed")
                    self.rest += chunk
                    continue
                h1, h2 = self.rest[0], self.rest[1]
                self.fin = (h1 & 0x80) != 0
                self.opcode = h1 & 0x0F
                self.has_mask = (h2 & 0x80) != 0
                
                if self.has_mask:
                    self.send_frame(8, struct.pack("!H", 1002))
                    raise ConnectionError("Protocol error: masked frame from server")
                    
                self.length = h2 & 0x7F
                self.rest = self.rest[2:]
                self.state = "LENGTH"
                
            elif self.state == "LENGTH":
                if self.length == 126:
                    if len(self.rest) < 2:
                        chunk = self.sock.recv(4096)
                        if not chunk: raise ConnectionError("Closed")
                        self.rest += chunk
                        continue
                    self.length = struct.unpack("!H", self.rest[:2])[0]
                    self.rest = self.rest[2:]
                elif self.length == 127:
                    if len(self.rest) < 8:
                        chunk = self.sock.recv(4096)
                        if not chunk: raise ConnectionError("Closed")
                        self.rest += chunk
                        continue
                    self.length = struct.unpack("!Q", self.rest[:8])[0]
                    self.rest = self.rest[8:]
                
                if self.length > 10 * 1024 * 1024:
                    self.send_frame(8, struct.pack("!H", 1009))
                    raise ConnectionError("Message too big")
                    
                self.state = "MASK"
                
            elif self.state == "MASK":
                if self.has_mask:
                    if len(self.rest) < 4:
                        chunk = self.sock.recv(4096)
                        if not chunk: raise ConnectionError("Closed")
                        self.rest += chunk
                        continue
                    self.mask = self.rest[:4]
                    self.rest = self.rest[4:]
                else:
                    self.mask = None
                self.payload = bytearray()
                self.state = "PAYLOAD"
                
            elif self.state == "PAYLOAD":
                needed = self.length - len(self.payload)
                if needed > 0:
                    avail = min(needed, len(self.rest))
                    if avail > 0:
                        self.payload.extend(self.rest[:avail])
                        self.rest = self.rest[avail:]
                        needed -= avail
                    if needed > 0:
                        chunk = self.sock.recv(min(needed, 4096))
                        if not chunk: raise ConnectionError("Closed")
                        self.payload.extend(chunk)
                        continue
                        
                if self.mask:
                    for i in range(len(self.payload)):
                        self.payload[i] ^= self.mask[i % 4]
                        
                fin, opcode, payload = self.fin, self.opcode, bytes(self.payload)
                self.state = "HEADER"
                return fin, opcode, payload
