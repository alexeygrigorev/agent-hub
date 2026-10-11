import struct
import socket

class WSConnection:
    def __init__(self, sock, rest: bytes, rng):
        self.sock = sock
        self.rest = bytearray(rest)
        self.rng = rng
        self.state = "HEADER"
        self.fin = False
        self.opcode = 0
        self.has_mask = False
        self.length = 0
        self.mask = None
        self.payload = bytearray()
        
        self.msg_opcode = None
        self.msg_payload = bytearray()

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

    def _fail(self, code: int, msg: str):
        try:
            self.send_frame(8, struct.pack("!H", code))
        except Exception:
            pass
        raise ConnectionError(msg)

    def recv_frame(self):
        while True:
            if self.state == "HEADER":
                if len(self.rest) < 2:
                    chunk = self.sock.recv(4096)
                    if not chunk: raise ConnectionError("Closed")
                    self.rest.extend(chunk)
                    continue
                h1, h2 = self.rest[0], self.rest[1]
                self.fin = (h1 & 0x80) != 0
                rsv1 = (h1 & 0x40) != 0
                rsv2 = (h1 & 0x20) != 0
                rsv3 = (h1 & 0x10) != 0
                self.opcode = h1 & 0x0F
                self.has_mask = (h2 & 0x80) != 0
                
                if rsv1 or rsv2 or rsv3:
                    self._fail(1002, "RSV bits set")
                
                if self.opcode >= 8:
                    if not self.fin:
                        self._fail(1002, "Fragmented control frame")
                else:
                    if self.opcode not in (0, 1, 2):
                        self._fail(1002, "Reserved opcode")
                
                if self.opcode >= 8 and self.opcode not in (8, 9, 10):
                    self._fail(1002, "Reserved control opcode")

                if self.has_mask:
                    self._fail(1002, "Masked frame from server")
                    
                self.length = h2 & 0x7F
                if self.opcode >= 8 and self.length > 125:
                    self._fail(1002, "Oversized control frame")

                self.rest = self.rest[2:]
                self.state = "LENGTH"
                
            elif self.state == "LENGTH":
                if self.length == 126:
                    if len(self.rest) < 2:
                        chunk = self.sock.recv(4096)
                        if not chunk: raise ConnectionError("Closed")
                        self.rest.extend(chunk)
                        continue
                    self.length = struct.unpack("!H", self.rest[:2])[0]
                    if self.length < 126:
                        self._fail(1002, "Invalid length encoding")
                    self.rest = self.rest[2:]
                elif self.length == 127:
                    if len(self.rest) < 8:
                        chunk = self.sock.recv(4096)
                        if not chunk: raise ConnectionError("Closed")
                        self.rest.extend(chunk)
                        continue
                    self.length = struct.unpack("!Q", self.rest[:8])[0]
                    if self.length < 65536:
                        self._fail(1002, "Invalid length encoding")
                    if self.length & 0x8000000000000000:
                        self._fail(1002, "High bit of 64-bit length set")
                    self.rest = self.rest[8:]
                
                self.state = "MASK"
                
            elif self.state == "MASK":
                if self.has_mask:
                    if len(self.rest) < 4:
                        chunk = self.sock.recv(4096)
                        if not chunk: raise ConnectionError("Closed")
                        self.rest.extend(chunk)
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

                if opcode >= 8: # Control frame
                    if opcode == 8: # Close
                        if len(payload) == 1:
                            self._fail(1002, "Close frame payload length 1")
                        code = 1000
                        if len(payload) >= 2:
                            code = struct.unpack("!H", payload[:2])[0]
                            if code < 1000 or code in (1004, 1005, 1006, 1015) or (1012 <= code <= 1014) or (code >= 5000):
                                self._fail(1002, "Invalid close code")
                            if len(payload) > 2:
                                try:
                                    payload[2:].decode('utf-8')
                                except UnicodeDecodeError:
                                    self._fail(1007, "Invalid UTF-8 in close payload")
                        return fin, opcode, payload
                    return fin, opcode, payload

                # Data frames
                if opcode == 0:
                    if self.msg_opcode is None:
                        self._fail(1002, "Continuation without start")
                else:
                    if self.msg_opcode is not None:
                        self._fail(1002, "New data frame while fragment open")
                    self.msg_opcode = opcode

                if len(self.msg_payload) + len(payload) > 10 * 1024 * 1024:
                    self._fail(1009, "Message too big")
                
                self.msg_payload.extend(payload)

                if fin:
                    if self.msg_opcode == 1: # Text
                        try:
                            self.msg_payload.decode('utf-8')
                        except UnicodeDecodeError:
                            self._fail(1007, "Invalid UTF-8")
                    
                    ret_opcode = self.msg_opcode
                    ret_payload = bytes(self.msg_payload)
                    self.msg_opcode = None
                    self.msg_payload = bytearray()
                    return True, ret_opcode, ret_payload
                else:
                    continue
