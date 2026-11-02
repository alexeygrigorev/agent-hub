#!/usr/bin/env python3
"""Count new processes over a window and attribute them to callers.

Polls /proc, so short-lived processes can be missed; use the same method before
and after a change and compare. Usage: measure-spawns.py [seconds] [--json]
"""
import collections, json, os, sys, time

def stat(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            s = f.read()
        r = s.rindex(")")
        comm = s[s.index("(") + 1:r]
        rest = s[r + 2:].split()
        return comm, int(rest[1]), int(rest[19])  # ppid, starttime
    except Exception:
        return None

def cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except Exception:
        return ""

def label(cmd):
    p = cmd.split()
    if not p:
        return "?"
    b = os.path.basename(p[0])
    if b in ("python", "python3", "node", "bash", "sh") and len(p) > 1:
        return b + " " + " ".join(os.path.basename(x) if "/" in x else x for x in p[1:3])
    return " ".join([b] + [x for x in p[1:3] if not x.startswith("-") or x in ("--all", "--json")])

def main():
    secs = float(next((a for a in sys.argv[1:] if not a.startswith("--")), 60))
    seen = {}
    cache = {}
    end = time.time() + secs
    cpu0 = open("/proc/stat").readline().split()
    while time.time() < end:
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            pid = int(d)
            st = stat(pid)
            if st is None:
                continue
            key = (pid, st[2])
            if key in seen:
                continue
            seen[key] = (st[0], st[1], cmdline(pid))
            cache[pid] = (st[0], st[1], seen[key][2])
        time.sleep(0.02)
    cpu1 = open("/proc/stat").readline().split()
    # skip processes already present at start
    d0 = [int(x) for x in cpu0[1:]]; d1 = [int(x) for x in cpu1[1:]]
    tot = sum(d1) - sum(d0)
    sysp = 100.0 * (d1[2] - d0[2]) / tot if tot else 0
    by_cmd = collections.Counter(); by_caller = collections.Counter(); aplexer_callers = collections.Counter()
    first = True
    for (pid, _), (comm, ppid, cmd) in seen.items():
        by_cmd[label(cmd) if cmd else comm] += 1
        # nearest ancestor that is not a shell/wrapper
        cur, caller = ppid, "init"
        for _ in range(8):
            c = cache.get(cur) or (lambda s: (s[0], s[1], cmdline(cur)) if s else None)(stat(cur))
            if not c:
                break
            if c[0] not in ("bash", "sh", "dash", "env", "timeout", "systemd-run", "flock"):
                caller = label(c[2]) if c[2] else c[0]
                break
            cur = c[1]
        by_caller[caller] += 1
        if (" ".join(label(cmd).split()[:3]).startswith(("aplexer list", "aplexer context hook", "aplexer message log"))):
            aplexer_callers[(label(cmd), caller)] += 1
    out = {"seconds": secs, "new_processes": sum(by_cmd.values()),
           "per_30s": round(sum(by_cmd.values()) * 30 / secs, 1), "sys_cpu_pct": round(sysp, 1),
           "aplexer_list": sum(v for k, v in by_cmd.items() if k.startswith("aplexer list")),
           "aplexer_context_hook": sum(v for k, v in by_cmd.items() if k.startswith("aplexer context hook")),
           "aplexer_message_log": sum(v for k, v in by_cmd.items() if k.startswith("aplexer message log")),
           "aplexer_callers": [[" -> ".join(k), v] for k, v in aplexer_callers.most_common(20)], "top_commands": by_cmd.most_common(15), "top_callers": by_caller.most_common(15)}
    print(json.dumps(out, indent=1))

main()
