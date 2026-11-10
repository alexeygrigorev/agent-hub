"""Bounded TLS text transport using the pinned maintained RFC 6455 implementation."""
import logging
import socket
from urllib.parse import urlsplit
from websockets.exceptions import WebSocketException, InvalidStatus
from .client import response_error
from websockets.sync.client import connect


class WebSocket:
    def __init__(self,endpoint,token,timeout=30):
        uri=urlsplit(endpoint)
        if uri.scheme!='wss' and (uri.scheme!='ws' or uri.hostname not in {'localhost','127.0.0.1','::1'}):
            raise ValueError('secure_websocket_required')
        if uri.username or uri.password or uri.fragment or any(ord(c)<32 for c in endpoint+token):
            raise ValueError('invalid_websocket_credentials')
        raw=socket.create_connection((uri.hostname,uri.port or (443 if uri.scheme=='wss' else 80)),timeout)
        try:
            raw.setsockopt(socket.SOL_SOCKET,socket.SO_KEEPALIVE,1)
            if hasattr(socket,'TCP_KEEPIDLE'):raw.setsockopt(socket.IPPROTO_TCP,socket.TCP_KEEPIDLE,60)
            if hasattr(socket,'SIO_KEEPALIVE_VALS'):raw.ioctl(socket.SIO_KEEPALIVE_VALS,(1,60000,10000))
            self.connection=connect(endpoint,sock=raw,additional_headers={'Authorization':'Bearer '+token},
                compression=None,proxy=None,ping_interval=20,ping_timeout=40,open_timeout=timeout,close_timeout=1,
                max_size=65536,max_queue=4,logger=logging.Logger('hub2.transport',level=logging.CRITICAL+1))
        except InvalidStatus as error:
            raw.close()
            raise response_error(error.response.status_code,error.response.body) from None
        except Exception:
            raw.close()
            raise RuntimeError('websocket_connection_unavailable') from None

    def send(self,text):
        if not isinstance(text,str) or len(text.encode())>65536:raise ValueError('websocket_text_too_large')
        try:self.connection.send(text)
        except WebSocketException:raise RuntimeError('websocket_send_unavailable') from None

    def recv(self,timeout=None):
        try:value=self.connection.recv(timeout=timeout)
        except WebSocketException:raise RuntimeError('websocket_receive_unavailable') from None
        if not isinstance(value,str):raise RuntimeError('websocket_text_required')
        return value

    def close(self):
        self.connection.close()

    def __enter__(self):return self
    def __exit__(self,*_):self.close()
