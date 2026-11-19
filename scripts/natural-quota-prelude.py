"""INACTIVE production-only same-PID prelude. No CLI selection is implemented.

A future reviewed immutable Root artifact must bind SelectedQuota and source-open
callback. Nothing reads producer env/config, starts business children, creates a
watch or connects on import/default execution. No fixture guard/path is used.
"""
import hashlib
import json
import math
import time
import types
from dataclasses import dataclass


@dataclass(frozen=True)
class SelectedQuota:
    role: str
    quota: bytes
    quota_sha256: str
    client: bytes
    client_sha256: str
    argv: tuple
    source_path: str | None = None
    client_path: str | None = None
    def checked_business(self):
        # Only this boundary may prevent execution: original immutable business
        # bytes and fixed original argv must be independently trusted.
        if self.role not in ('quota_publish','quota_monitor') or type(self.argv) is not tuple:
            raise ValueError('selection_unavailable')
        self._bytes(self.quota,self.quota_sha256)
        if self.role=='quota_publish' and self.argv!=():raise ValueError('selection_unavailable')
        if self.role=='quota_monitor' and not (len(self.argv)==8 and self.argv[:3]==('--check','--health-check','--health-config')
            and type(self.argv[3]) is str and self.argv[3].startswith('/')
            and self.argv[4:]==('--health-timeout','8','--alert-timeout','8')):raise ValueError('selection_unavailable')
        return self
    @staticmethod
    def _bytes(raw,digest):
        if (type(raw) is not bytes or not 0<len(raw)<=262144 or type(digest) is not str
                or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest)
                or hashlib.sha256(raw).hexdigest()!=digest):
            raise ValueError('selection_unavailable')
    def checked_observer(self):
        self._bytes(self.client,self.client_sha256)
        return self
    def checked(self):
        # Preserve strict explicit full-selection inspection; run_selected uses
        # split boundaries so optional companion faults never gate business.
        self.checked_business();return self.checked_observer()



class Observer:
    def __init__(self,child):self.child=child
    def fail(self):self.child.dirty=True
    def emit(self,*a,**k):return self.child.emit(*a,**k)


def run_selected(selected,source_open,channel_factory,*,now=time.monotonic,now_ns=time.monotonic_ns,started_ns=None):
    """INTERNAL immutable caller only, NOT authority from a SelectedQuota object.

    Manager independently selects unit/role/live source/interpreter/channel. These
    bytes must already be Root-bound by the missing startup artifact; no path or
    producer dictionary can activate the resident adapter. No shell/subprocess.
    """
    try:started=now_ns() if started_ns is None else started_ns
    except Exception:started=None
    if type(selected) is not SelectedQuota:raise ValueError('selection_unavailable')
    selected.checked_business()
    valid=type(started) is int and started>0
    end=started/1e9+.15 if valid else 0.;last=started/1e9 if valid else 0.
    def fresh():
        nonlocal last
        current=now()
        if type(current) not in (int,float) or not math.isfinite(current) or current<last or current>=end:raise TimeoutError('observation_unavailable')
        last=current
    channel=None;context=None;observer=None;original=None
    try:
        try:
            selected.checked_observer()
            fresh();channel=channel_factory(end) # no helper spawn or role env
            channel.send(json.dumps({'v':1,'op':'held','started_ns':started},separators=(',',':')).encode(),end)
            raw=channel.recv(end);fresh()
            if not (type(raw) is bytes and len(raw)==37 and raw.startswith(b'open:')):raise ValueError('observation_unavailable')
            context=raw[5:].decode('ascii')
            if any(c not in '0123456789abcdef' for c in context):raise ValueError('observation_unavailable')
            # Actual Root selected source FD/open+hash. The observer's real mark
            # corroborates this independently; callback text is never admission.
            source_open(selected);fresh()
            channel.send(b'quota-source-opened',end)
            if channel.recv(end)!=b'qualified-run':raise ValueError('observation_unavailable')
            fresh()
            client=types.ModuleType('natural_selected_client')
            if selected.client_path is not None:client.__file__=selected.client_path
            exec(compile(selected.client,'<immutable-natural-client>','exec'),client.__dict__)
            def exchange(_path,request,*,deadline=None):
                nonlocal last
                current=now()
                if type(current) not in (int,float) or not math.isfinite(current) or current<last:raise TimeoutError('observation_unavailable')
                last=current;stop=current+.15
                if deadline is not None:
                    if type(deadline) not in (int,float) or not math.isfinite(deadline):raise TimeoutError('observation_unavailable')
                    stop=min(stop,deadline)
                if current>=stop:raise TimeoutError('observation_unavailable')
                channel.send(client.encode(request,client.FRAME),stop)
                reply=client.decode(channel.recv(stop),client.REPLY)
                ended=now()
                if type(ended) not in (int,float) or not math.isfinite(ended) or ended<last or ended>=stop:raise TimeoutError('observation_unavailable')
                last=ended;return reply
            client.exchange=exchange
            observer=Observer(client.Client(None,context,None,selected.role))
        except Exception:
            # Never retry observation/start or run a replaced file on failure.
            observer=None
        program=types.ModuleType('natural_selected_quota')
        program.__file__=selected.source_path or '<immutable-natural-quota>'
        exec(compile(selected.quota,program.__file__,'exec'),program.__dict__)
        return program.main(list(selected.argv),_managed_observer=observer)
    except BaseException as error:
        original=error
        raise
    finally:
        if channel is not None:
            try:channel.close()
            except BaseException as error:
                if observer is not None:observer.fail()
                if original is None and not isinstance(error,Exception):raise


if __name__=='__main__':
    print(json.dumps({'status':'INERT_HOLD','authority':'NONE','complete':False,
        'missing':'reviewed immutable startup selection and resource/exit-retention adapter'}))
