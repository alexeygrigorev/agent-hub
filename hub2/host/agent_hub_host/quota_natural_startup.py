"""Trusted natural-quota descriptor donation receiver.

Only a fresh, read-only ROOT-owned pipe can enter this path. Configuration,
producer frames and retained receipt dictionaries are not donation authority.
This module neither creates privileged watches nor starts quota services.
"""
import fcntl
import json
import os
from pathlib import Path
import select
import stat
import time

from . import alert_authority as ram
from . import quota_dispatch_manager as m
from . import quota_natural_primary as primary


FIELDS = {'v', 'kind', 'target_uid', 'handoff_deadline_ns', 'ack_fd',
          'source_directory', 'selection_path', 'broker_directory',
          'gate_directory', 'interpreter', 'entry', 'selection', 'source',
          'client', 'fan_group', 'gate_sock_fd', 'gate_dir_fd', 'gate_node_fd',
          'gate_node'}


def _read_root_pipe(fd):
    ram.require(type(fd) is int and fd > 2)
    info = os.fstat(fd)
    # A rejected unrelated descriptor is not ours to close.
    ram.require(stat.S_ISFIFO(info.st_mode) and info.st_uid == 0
                and fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY)
    os.set_blocking(fd, False)
    deadline = time.monotonic() + 5
    chunks = []
    size = 0
    try:
        while size <= 65536:
            remaining = deadline - time.monotonic()
            ram.require(remaining > 0 and select.select([fd], [], [], remaining)[0])
            try:
                raw = os.read(fd, 65537 - size)
            except BlockingIOError:
                continue
            if not raw:
                break
            chunks.append(raw)
            size += len(raw)
    finally:
        os.close(fd)
    ram.require(0 < size <= 65536)
    value = json.loads(b''.join(chunks), object_pairs_hook=primary._unique,
                       parse_constant=lambda _: ram.require(False))
    ram.require(type(value) is dict and set(value) == FIELDS
                and type(value['v']) is int and value['v'] == 2
                and value['kind'] == 'natural_quota_root_donation_v1'
                and type(value['target_uid']) is int
                and value['target_uid'] == os.getuid() > 0
                and type(value['handoff_deadline_ns']) is int
                and time.monotonic_ns() < value['handoff_deadline_ns'])
    return value


def _descriptor_list(value):
    result = [value[k].get('fd') for k in ('interpreter', 'entry')]
    for key in ('selection', 'source', 'client'):
        ram.require(type(value[key]) is dict)
        result.extend((value[key].get('fd'), value[key].get('dir_fd')))
    result.extend(value[k] for k in
        ('fan_group', 'gate_sock_fd', 'gate_dir_fd', 'gate_node_fd'))
    if value['ack_fd'] is not None:result.append(value['ack_fd'])
    ram.require(all(type(fd) is int and fd > 2 for fd in result)
                and len(set(result)) == len(result))
    return result


def _root_graph(paths):
    """Acquire our directory references, retaining every branch until cleanup."""
    nodes = {}
    edges = []
    try:
        root = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        nodes['/'] = root
        for path in paths:
            p = primary._path(str(path))
            current = Path('/')
            for name in p.parts[1:]:
                parent = nodes[str(current)]
                current = current / name
                if str(current) not in nodes:
                    ram.require(len(nodes)<ram.MAX_ACTIVE)
                    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY
                                 | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
                    nodes[str(current)] = fd
                    edges.append((parent, name, fd))
        directories = m._natural_directories(tuple(edges))
        ram.require(all((os.fstat(fd).st_dev, os.fstat(fd).st_ino)
                        in directories for fd in nodes.values()))
        return nodes, tuple(edges)
    except BaseException:
        for fd in reversed(tuple(nodes.values())):
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def _image(section, nodes, *, executable):
    ram.require(type(section) is dict)
    fd = section.get('fd')
    ram.require(type(fd) is int and fd > 2
                and fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY)
    value = m._verify_pinned_root_file(section, require_exec=executable)
    m.StableRuntime.verify_root_owned_file(value['path'], value)
    info = os.fstat(fd)
    raw = os.pread(fd, info.st_size + 1, 0)
    ram.require(len(raw) == info.st_size)
    path = Path(value['path'])
    return m.HeldExecutableFD(fd, nodes[str(path.parent)], path.name, raw,
        m.StableRuntime.source_stamp(info),
        ram.SourcePin(info.st_dev, info.st_ino, value['sha256']))


def _directory_is(actual, expected):
    a, b = os.fstat(actual), os.fstat(expected)
    ram.require((a.st_dev, a.st_ino) == (b.st_dev, b.st_ino))


def _child_interpreter(held, path):
    """Canonical child exec name for the already checked Root interpreter.

    The initial Host still execs the exact donated descriptor. A subsequent
    subprocess closes inherited FDs, so its interpreter must have a verified
    persistent name, bound to that descriptor AND this process's actual image.
    Root-connected ancestry and full bytes were checked by the selection.
    """
    ram.require(type(held) is m.HeldExecutableFD)
    primary._path(path)
    info = os.fstat(held.fd)
    m._natural_held_pin(held, info)
    ram.require(m._natural_stamp(info) == m._natural_stamp(os.stat(path, follow_symlinks=False))
                == m._natural_stamp(os.stat('/proc/self/exe')))
    return path


def _notification_only_group(fd,source):
    m._verify_fanotify_fd(fd,source.pin.dev,source.pin.ino)
    with open(f'/proc/self/fdinfo/{fd}','r',encoding='ascii')as stream:
        raw=stream.read(8193)
    ram.require(len(raw)<=8192)
    headers=[line for line in raw.splitlines() if line.startswith('fanotify flags:')]
    marks=[line for line in raw.splitlines() if line.startswith('fanotify ino:')]
    ram.require(len(headers)==1 and len(marks)==1
        and not any(line.startswith(('fanotify mnt_id:','fanotify fsid:')) for line in raw.splitlines()))
    flags=int(headers[0].split()[1].split(':',1)[1],16)
    ram.require(flags&0x0c==0) # FAN_CLASS_CONTENT/PRE_CONTENT are not notification-only.
    values=dict(part.split(':',1) for part in marks[0].split()[1:] if ':' in part)
    ram.require(int(values.get('mask','0'),16)==0x20) # only FAN_OPEN, not permission events


def _close_gate(gate):
    """Strict, once-only retirement of THIS donated gate, not best-effort reuse."""
    errors=[]
    if gate.node_identity is not None:
        try:
            node=os.stat('source-gate.sock',dir_fd=gate.dir_fd,follow_symlinks=False)
            ram.require((node.st_dev,node.st_ino)==gate.node_identity)
            os.unlink('source-gate.sock',dir_fd=gate.dir_fd)
        except BaseException as error:errors.append(error)
        gate.node_identity=None
    listener,gate.listener=gate.listener,None
    if listener is not None:
        try:listener.close()
        except BaseException as error:errors.append(error)
    for name in ('node_fd','dir_fd'):
        fd=getattr(gate,name);setattr(gate,name,None)
        if fd is not None:
            try:os.close(fd)
            except BaseException as error:errors.append(error)
    if errors:raise errors[0]


class NaturalDonation:
    """Opaque local lifetime owner constructed only by the Root-pipe receiver."""
    def __init__(self, *a, **k):
        raise ram.Unknown('root_donation_required')

    def start(self):
        ram.require(not self.started and not self.closed)
        self.started = True
        from .alert_broker import Broker
        try:
            broker = Broker._selected_natural_quota(
                self.broker_directory, self.selection, self.gate, self.group,
                on_resources_closed=self.close,
                on_owner_exit=self._owner_exit,on_manager=self._manager_started)
            self.broker = broker
            broker.start()
        except BaseException:
            try:self._owner_exit()
            except BaseException:pass
            raise
        return broker

    def _manager_started(self,manager):
        self.manager=manager

    def _owner_exit(self):
        if self.manager is None:self.close()
        else:self.manager.close()

    def close(self):
        if self.closed:
            if self.close_error is not None:raise self.close_error
            return
        self.closed = True
        # Called after the existing owner/probe has stopped using borrowed FDs.
        errors = []
        if self.gate is not None:
            gate, self.gate = self.gate, None
            try:
                _close_gate(gate)
            except BaseException as error:
                errors.append(error)
        for fd in reversed(self.fds):
            try:
                os.close(fd)
            except BaseException as error:
                errors.append(error)
        self.fds.clear()
        if errors:
            self.close_error=errors[0]
            raise errors[0]


def consume_natural_handoff(fd):
    """Adopt real Root descriptors, never a producer's serialized source claim."""
    value = _read_root_pipe(fd)
    for key in ('interpreter', 'entry'):
        ram.require(type(value[key]) is dict)
    fds = _descriptor_list(value)
    nodes = {}
    gate = None
    try:
        # The authenticated, distinct descriptor set is ours before any probe
        # can fail. Check existence before opening directories, so a missing
        # number cannot be reused and mistaken for a valid donated descriptor.
        for donated_fd in fds:
            os.fstat(donated_fd)
        for key in ('source_directory', 'selection_path', 'broker_directory', 'gate_directory'):
            primary._path(value[key])
        paths = [Path(value['source_directory']), Path(value['selection_path']).parent,
                 Path(value['interpreter']['path']).parent, Path(value['entry']['path']).parent]
        nodes, edges = _root_graph(paths)
        interpreter = _image(value['interpreter'], nodes, executable=True)
        entry = _image(value['entry'], nodes, executable=False)
        source = m._adopt_held_source(value['source'])
        client = m._adopt_held_source(value['client'])
        selection_file = m._adopt_held_source(value['selection'])
        source_directory = Path(value['source_directory'])
        ram.require(source.name == 'hub-quota-publish.py' and client.name == 'hub_alert_client.py'
                    and selection_file.name == Path(value['selection_path']).name)
        _directory_is(source.directory_fd, nodes[str(source_directory)])
        _directory_is(client.directory_fd, nodes[str(source_directory)])
        _directory_is(selection_file.directory_fd, nodes[str(Path(value['selection_path']).parent)])
        plan = primary.read_selection(value['selection_path'], selection_file.pin.sha256)
        ram.require(plan['quota_path'] == str(source_directory / source.name)
                    and plan['client_path'] == str(source_directory / client.name)
                    and plan['quota_sha256'] == source.pin.sha256
                    and plan['client_sha256'] == client.pin.sha256
                    and plan['gate_directory'] == value['gate_directory'])
        roles = tuple(m.NaturalRole(role, source, entry, client, interpreter,
            (value['interpreter']['path'], '-I', value['entry']['path'],
             'natural-primary', value['selection_path'], selection_file.pin.sha256, role),
            tuple(plan['roles'][role]['argv']), edges) for role in m.NATURAL_UNITS)
        selected = m.PeriodicQuotaSelection._checked_roles(
            roles, context_sources=(selection_file,))
        child_interpreter = _child_interpreter(interpreter, value['interpreter']['path'])
        _notification_only_group(value['fan_group'],source)
        gate = m.SourceGate.from_inherited(value['gate_directory'],
            value['gate_dir_fd'], value['gate_sock_fd'], value['gate_node_fd'], value['gate_node'])
        ack = value['ack_fd']
        ram.require(time.monotonic_ns()<value['handoff_deadline_ns'])
        if ack is not None:
            info = os.fstat(ack)
            ram.require(stat.S_ISFIFO(info.st_mode) and info.st_uid == 0
                        and fcntl.fcntl(ack, fcntl.F_GETFL) & os.O_ACCMODE == os.O_WRONLY)
            os.set_blocking(ack,False)
            ram.require(os.write(ack, b'ACK\n') == 4)
            fds.remove(ack) # never retry a possibly consumed descriptor
            os.close(ack)
        excluded = {value['gate_dir_fd'], value['gate_sock_fd'], value['gate_node_fd']}
        donation = object.__new__(NaturalDonation)
        donation.selection, donation.gate = selected, gate
        donation.group, donation.broker_directory = value['fan_group'], value['broker_directory']
        donation.fds = [n for n in fds if n not in excluded] + list(nodes.values())
        donation.started = donation.closed = False
        donation.close_error = None
        donation.manager = None
        donation.broker = None
        donation.child_interpreter = child_interpreter
        return donation
    except BaseException:
        excluded = set()
        if gate is not None:
            excluded.update((value['gate_dir_fd'], value['gate_sock_fd'], value['gate_node_fd']))
            try:_close_gate(gate)
            except BaseException:pass
        for item in reversed(tuple(dict.fromkeys(fds + list(nodes.values())))):
            if item not in excluded:
                try:
                    os.close(item)
                except OSError:
                    pass
        raise
