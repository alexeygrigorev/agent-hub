"""Linux resources borrowed by the resident natural-quota dispatcher.

This module never selects sources, creates watches, launches units or installs
itself. A trusted startup caller must donate the already checked selection,
listener and notification group. Producer frames cannot construct this adapter.
Global coverage remains unknown; unselected producers are not qualified here.
"""
import os
import socket
import struct
import time
from dataclasses import dataclass

from . import alert_authority as ram
from . import quota_dispatch_manager as m


@dataclass
class _OwnedLease:
    lease: m.NaturalLease
    role: str
    cgroup: str
    start_us: str
    event: object = None
    opened: bool = False
    status: int | None = None


class LinuxNaturalResources(m.NaturalResources):
    """Real, bounded OS probes; no fixture start/stop/cleanup closures.

    Metadata subprocesses run only from the manager's serial probe. Owner-thread
    current/open/exit operations use held FDs and bounded local proc metadata.
    Listener, sources and fanotify group remain owned by the startup caller.
    """
    PROPERTIES = ('MainPID', 'ControlGroup', 'InvocationID', 'ActiveState',
                  'SubState', 'Job', 'ExecStart', 'ExecMainCode',
                  'ExecMainStatus', 'ExecMainStartTimestampMonotonic')

    def __init__(self, selection, gate, fan, group, observation, *,
                 now=time.monotonic):
        ram.require(type(selection) is m.PeriodicQuotaSelection
                    and type(gate) is m.SourceGate
                    and type(fan) is m.FanotifyReader
                    and type(observation) is m.FanotifyObservation
                    and type(group) is int and group > 2
                    and os.getuid() > 0 and callable(now))
        self.selection, self.gate = selection, gate
        self.fan, self.group, self.observation = fan, group, observation
        self.now = now
        self.stable = m.StableRuntime()
        self.environment = m.UserManagerEnvironment(now=now)
        self.runner = m.SubprocessRunner()
        self.channels, self.owned = [], []
        self.fault = self.closed = False
        self.retirement_failed = False
        for plan in selection.roles.values():
            selection.current(plan, full=True)

    def _ready(self):
        ram.require(not self.closed and not self.fault
                    and not self.observation.unknown)

    def accept(self):
        self._ready()
        ram.require(len(self.channels) < ram.MAX_ACTIVE)
        channel = self.gate.poll_held()
        if channel is not None:
            self.channels.append(channel)  # ownership before validation
        return channel

    def _peer(self, channel):
        ram.require(any(c is channel for c in self.channels))
        peer = struct.unpack('3i', channel.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        ram.require(peer[0] > 0 and peer[1] == self.stable.UID)
        return peer[0]

    def _show(self, role, deadline):
        """Fixed unit/property query, never caller-supplied argv or facts."""
        ram.require(role in m.NATURAL_UNITS)
        rc, raw = self.runner.run(
            ['/usr/bin/systemctl', '--user', 'show', '--all', m.NATURAL_UNITS[role],
             '--property=' + ','.join(self.PROPERTIES)],
            self.environment.remaining(deadline))
        ram.require(rc == 0 and type(raw) is bytes and 0 < len(raw) <= 16384)
        values = {}
        for line in raw.decode('utf-8', 'strict').splitlines():
            ram.require('=' in line)
            key, value = line.split('=', 1)
            ram.require(key in self.PROPERTIES and key not in values)
            values[key] = value
        ram.require(set(values) == set(self.PROPERTIES))
        return values

    @staticmethod
    def _invocation(state):
        value = state['InvocationID']
        ram.require(ram.identity(value))
        return value

    def _matching_unit(self, pid, deadline):
        matches = []
        for role in m.NATURAL_UNITS:
            state = self._show(role, deadline)
            if state['MainPID'] == str(pid):
                matches.append((role, state))
        ram.require(len(matches) == 1)
        return matches[0]

    def _exec(self, state, plan):
        # V4's maintained parser rejects a changed executable or argv.
        self.stable.inspect_exec(('ExecStart=' + state['ExecStart']).encode(),
                                 ' '.join(plan.exec_start))

    @staticmethod
    def _command(pid, plan):
        # Printed ExecStart alone cannot distinguish argv elements with spaces.
        with open(f'/proc/{pid}/cmdline', 'rb') as stream:
            raw = stream.read(8193)
        ram.require(0 < len(raw) <= 8192 and raw.endswith(b'\0'))
        ram.require(tuple(s.decode('utf-8', 'strict')
                          for s in raw[:-1].split(b'\0')) == plan.exec_start)

    def bind(self, channel, started_ns, deadline):
        self._ready()
        ram.require(deadline == m.original_hold(started_ns, self.now()))
        pid = self._peer(channel)
        role, state = self._matching_unit(pid, deadline)
        plan = self.selection.roles[role]
        self.selection.current(plan, full=True)
        invocation = self._invocation(state)
        job = state['Job']
        # systemd v255 src/systemctl/systemctl-show.c:1022-1032 prints the
        # Job (uo) property as its positive decimal ID, empty if none.
        ram.require(job.isascii() and job.isdecimal() and 0 < int(job) < 2**32
                    and state['ActiveState'] in ('activating', 'active')
                    and state['SubState'] in ('start', 'running')
                    and state['ControlGroup'].startswith('/'))
        self._exec(state, plan)
        cgroup = state['ControlGroup']
        process = ram.ProcessKey(*self.stable.inspect_process(pid, cgroup)).checked()
        ram.require(not any(o.lease.process == process for o in self.owned))
        pidfd = os.pidfd_open(pid, 0)
        lease = m.NaturalLease(m.NATURAL_UNITS[role], int(job), invocation,
                               process, pidfd, channel, plan.source, started_ns)
        owned = _OwnedLease(lease, role, cgroup,
                            state['ExecMainStartTimestampMonotonic'])
        self.owned.append(owned)  # retain even if a later probe fails
        self.current(lease)
        ram.require(os.stat('/proc/self/ns/time').st_ino ==
                    os.stat(f'/proc/{pid}/ns/time').st_ino)
        exe = os.stat(f'/proc/{pid}/exe')
        ram.require((exe.st_dev, exe.st_ino) ==
                    (plan.interpreter.pin.dev, plan.interpreter.pin.ino))
        ram.require(owned.start_us.isascii() and owned.start_us.isdecimal())
        before = int(owned.start_us)
        ram.require(0 < before * 1000 <= started_ns)
        again = self._show(role, deadline)
        ram.require(again['MainPID'] == str(pid) and
                    again['InvocationID'] == invocation and again['Job'] == job
                    and again['ControlGroup'] == cgroup and
                    again['ExecMainStartTimestampMonotonic'] ==
                    state['ExecMainStartTimestampMonotonic'])
        self._exec(again, plan)
        self.selection.current(plan, full=True)
        self.current(lease)
        ram.require(self.now() < deadline)
        return lease

    def _owned(self, lease):
        self._ready()
        values = [o for o in self.owned if o.lease is lease]
        ram.require(len(values) == 1)
        return values[0]

    def current(self, lease):
        owned = self._owned(lease)
        self.selection.current(self.selection.roles[owned.role])
        ram.require(self.stable.alive(lease.pidfd)
                    and self._peer(lease.channel) == lease.process.pid
                    and self.stable.inspect_process(lease.process.pid, owned.cgroup)
                    == (lease.process.pid, lease.process.birth_ticks,
                        lease.process.boot_id))
        plan = self.selection.roles[owned.role]
        self._command(lease.process.pid, plan)
        exe = os.stat(f'/proc/{lease.process.pid}/exe')
        ram.require((exe.st_dev, exe.st_ino) ==
                    (plan.interpreter.pin.dev, plan.interpreter.pin.ino))

    def _route_events(self):
        try:
            self._ready()
            self.fan.drain(self.group, self.observation)
            ram.require(not self.observation.unknown)
            for event in self.observation.records:
                matches = [o for o in self.owned
                           if o.lease.process.pid == event.get('pid') and
                           (o.lease.source.pin.dev, o.lease.source.pin.ino) ==
                           (event.get('dev'), event.get('ino'))]
                ram.require(len(matches) == 1 and event.get('mask', 0) & 0x20)
                owned = matches[0]
                self.current(owned.lease)
                ram.require(owned.event is None and not owned.opened)
                owned.event = event
            # Move bounded notifications to their leases; never erase a fault.
            self.observation.records.clear()
        except BaseException:
            self.fault = True
            raise

    def drain(self):
        """Owner-thread bounded drain, including idle and post-open periods."""
        try:
            self._route_events()
            for plan in self.selection.roles.values():
                self.selection.current(plan)
        except BaseException:
            self.fault = True
            raise

    def source_event(self, lease):
        self.current(lease)
        self._route_events()
        return self._owned(lease).event

    def exact_open(self, lease, event):
        self.current(lease)
        owned = self._owned(lease)
        ram.require(event is not None and owned.event is event and not owned.opened)
        owned.opened = True
        owned.event = None

    def exited(self, lease):
        self._owned(lease)
        return not self.stable.alive(lease.pidfd)

    def retirement(self, lease):
        owned = self._owned(lease)
        ram.require(self.exited(lease) and owned.opened and owned.status is None)
        plan = self.selection.roles[owned.role]
        deadline = lease.started_ns / 1e9 + (45 if owned.role == 'quota_monitor' else 135)
        before = self._show(owned.role, deadline)
        ram.require(before['InvocationID'] == lease.invocation
                    and before['MainPID'] == '0'
                    and before['ControlGroup'] == owned.cgroup
                    and before['ExecMainStartTimestampMonotonic'] == owned.start_us
                    and before['ExecMainCode'] == '1'
                    and before['ExecMainStatus'].isascii()
                    and before['ExecMainStatus'].isdecimal()
                    and 0 <= int(before['ExecMainStatus']) <= 255)
        self._exec(before, plan)
        after = self._show(owned.role, deadline)
        keys = ('InvocationID', 'MainPID', 'ControlGroup', 'ExecMainCode',
                'ExecMainStatus', 'ExecMainStartTimestampMonotonic')
        ram.require(all(before[k] == after[k] for k in keys)
                    and self.exited(lease) and self.now() < deadline)
        self._exec(after, plan)
        self.selection.current(plan, full=True)
        owned.status = int(before['ExecMainStatus'])
        return lease, owned.status

    def retire_owned(self, lease):
        """Once-only local handle retirement AFTER same-invocation settlement.

        The serial metadata worker owns bind/retirement/close. Remove custody
        before each close attempt; never retry a possibly consumed FD number.
        This does not start/stop units, reset evidence, or close donated FDs.
        """
        owned = self._owned(lease)
        ram.require(type(owned.status) is int and owned.opened and owned.event is None
                    and lease.released and self.exited(lease)
                    and sum(c is lease.channel for c in self.channels) == 1)
        self.owned = [value for value in self.owned if value is not owned]
        self.channels = [channel for channel in self.channels if channel is not lease.channel]
        errors = []
        try:
            lease.channel.close()
        except BaseException as error:
            errors.append(error)
        try:
            os.close(lease.pidfd)
        except BaseException as error:
            errors.append(error)
        if errors:
            self.fault = True
            self.retirement_failed = True
            raise errors[0]
        return lease

    def close_owned(self, channels, leases):
        # Resource ownership comes from accept/pidfd_open, not these arguments.
        ram.require(not self.closed)
        self.closed = True
        errors = []
        if self.retirement_failed:
            errors.append(ram.Unknown('prior_resource_retirement_failed'))
        if (any(not any(c is real for real in self.channels) for c in channels)
                or any(not any(l is o.lease for o in self.owned) for l in leases)):
            errors.append(ram.Unknown('foreign_cleanup_resource'))
        for channel in self.channels:
            try:
                channel.close()
            except BaseException as error:
                errors.append(error)
        for owned in self.owned:
            try:
                os.close(owned.lease.pidfd)
            except BaseException as error:
                errors.append(error)
        if errors:
            self.fault = True
            raise errors[0]
