"""Hydrogen bounded display acquisition controller (pure; all I/O is injected).

Integrated from Root's private display-acquirer proposal V2 (source sha256
82cea75cf09b0e6bcf6bc68a34e65202b9e2d862ee914b07541c5926670fda6f). Identity
validation, deadlines, fence-before-send order, the single 0x0f byte, no replay,
at most two post-key captures and final identity/artifact rechecks are kept
verbatim. Integration differences (docs/HUB-ACCEPTANCE-10.md):

* The command grammar accepts every sample's exact `.local/h7_<12hex>_<NN>.py`
  path (the proposal pinned the single observed sample-1 identifier).
* The collapsed-aggregate eligibility row binds the clipped 20-character prefix
  of THIS sample's exact command and accepts a single-digit aggregate count
  `Ran N commands (+N-1 · ...)` (2 <= N <= 9). Every column, style, width and
  other row is unchanged from the observed frame; other layouts refuse.

The result `CANDIDATE_FRAME_RECOGNIZED` only means the injected, separately
reviewed expanded-frame recognizer matched; `native_authority` stays false here.
"""
from dataclasses import dataclass
import math
import re
import uuid
import unicodedata
import xml.etree.ElementTree as ET


# The one reviewed provider/renderer observation (AGY 1.5.7, Hydrogen medium).
MODEL = "gemini-4-hydrogen-medium"
PROVIDER_VERSION = "1.5.7"
PROVIDER_EXECUTABLE_SHA256 = "9db59b6371107b939befa9c507c0a24989fda974e75d81d8529f3689ef7322e5"


@dataclass(frozen=True)
class Identity:
    hub_session: str
    intake: str
    machine: str
    epoch: int
    operation: str
    request_digest: str
    native: str
    birth_ms: int
    tag: str
    workspace: str
    provider: str
    engine: str
    model: str
    version: str
    executable_sha256: str


@dataclass(frozen=True)
class Observation:
    identity: Identity
    live: bool
    current_assignment: bool
    known_human_intervention: bool


@dataclass(frozen=True)
class Snapshot:
    identity: Identity
    svg: bytes
    complete: bool


@dataclass(frozen=True)
class Intent:
    identity: Identity
    command: str
    # Trusted Root custody input, NOT a native/cryptographic attestation.
    root_owned: bool
    deadline: float


class Refused(Exception):
    pass


def _valid_identity(i):
    # Dataclass equality does not distinguish 7 from 7.0 or 1 from True.
    # Every callback identity needs the same exact shape/types as Root intent.
    if type(i) is not Identity:
        raise Refused("invalid_identity_shape")
    text_fields = ("hub_session", "intake", "machine", "operation", "request_digest",
                   "native", "tag", "workspace", "provider", "engine", "model",
                   "version", "executable_sha256")
    if set(vars(i)) != set(text_fields) | {"epoch", "birth_ms"}:
        raise Refused("invalid_identity_shape")
    for name in text_fields:
        value = getattr(i, name)
        if (type(value) is not str or not value or len(value.encode('utf-8')) > 4096
                or any(unicodedata.category(c).startswith('C') for c in value)):
            raise Refused("invalid_identity_scalar")
    if type(i.epoch) is not int or i.epoch < 1 or type(i.birth_ms) is not int or i.birth_ms < 1:
        raise Refused("invalid_identity_integer")
    for value in (i.hub_session, i.intake, i.native):
        if str(uuid.UUID(value)) != value:
            raise Refused("noncanonical_identity")
    base, separator, attempt = i.operation.partition(":")
    if (not separator or str(uuid.UUID(base)) != base or not attempt.isdecimal()
            or str(int(attempt)) != attempt):
        raise Refused("noncanonical_operation")
    if (i.provider != "antigravity" or i.engine != "antigravity"
            or i.model != MODEL or i.version != PROVIDER_VERSION
            or i.executable_sha256 != PROVIDER_EXECUTABLE_SHA256
            or not re.fullmatch(r"[0-9a-f]{64}", i.request_digest)
            or not i.workspace.startswith("/")):
        raise Refused("unsupported_identity")


def _valid_intent(intent):
    if type(intent) is not Intent:
        raise Refused("invalid_intent_shape")
    _valid_identity(intent.identity)
    if (type(intent.root_owned) is not bool or not intent.root_owned
            or type(intent.command) is not str
            or not COMMAND_RE.fullmatch(intent.command)):
        raise Refused("unsupported_intent")


NS = "http://www.w3.org/2000/svg"
COMMAND_RE = re.compile(r"python3 \.local/h7_[0-9a-f]{12}_(?:0[1-9]|10)\.py")
FINAL_CAPTION = "Format observation complete; awaiting Root STOP."
MODE = "Auto mode: edits & safe commands auto-approved (shift+tab to cycle)"
BORDER = "─" * 80


def observed_collapsed(svg, command):
    """Only obs2's 80x24 aggregate + idle composer shape; NOT execution proof.

    The aggregate count and the clipped command prefix are bound to THIS
    sample's exact command; nothing else is generalized.

    Unrecognized formats refuse. No prose search/ANSI stripping. Geometry and
    styles are observable UI features, not unforgeable origin authentication.
    """
    if (type(svg) is not bytes or len(svg) > 262144 or b"<!" in svg
            or b"<?" in svg or b"\x1b" in svg or b"\r" in svg):
        return False
    try:
        root = ET.fromstring(svg)
        if root.tag != "{" + NS + "}svg" or root.attrib != {
                "width": "748", "height": "624", "viewBox": "0 0 748 624",
                "font-family": "DejaVu Sans Mono,Menlo,Consolas,monospace"}:
            return False
        # Exact direct-child topology, not root.iter(): inherited hidden or
        # translated nested SVG/group content can never authorize a key.
        if (len(root) != 3 or root[0].tag != "{"+NS+"}title" or root[0].attrib
                or len(root[0]) or not root[0].text or len(root[0].text.encode()) > 4096
                or any(unicodedata.category(c).startswith('C') for c in root[0].text)
                or root[1].tag != "{"+NS+"}rect" or root[1].attrib != {
                    "width":"100%","height":"100%","fill":"#0c0c0c"}
                or len(root[1]) or root[1].text):
            return False
        group=root[2]
        if (group.tag != "{"+NS+"}g" or group.attrib != {
                "font-size":"15", "{http://www.w3.org/XML/1998/namespace}space":"preserve"}
                or not 1 <= len(group) <= 512):
            return False
        for parent in (root, group):
            if parent.text and parent.text.strip():
                return False
            if any(e.tail and e.tail.strip() for e in parent):
                return False
        rows = {}
        rects = [root[1].attrib]
        previous=(-1,-1)
        statuses=0
        for e in group:
            if e.tag == "{"+NS+"}rect":
                if len(e) or e.text:
                    return False
                rects.append(e.attrib)
                continue
            if e.tag != "{"+NS+"}text" or len(e):
                return False
            status=e.attrib == {"x":"14","y":"605","fill":"#c8cdd4"}
            if not status and set(e.attrib) not in (
                    {"x","y","fill","textLength"},
                    {"x","y","fill","textLength","font-weight"}):
                return False
            if e.attrib.get("font-weight") not in (None,"bold"):
                return False
            if not all(re.fullmatch(r"0|[1-9][0-9]{0,3}", e.attrib[k]) for k in ("x","y")):
                return False
            x, y = int(e.attrib["x"]), int(e.attrib["y"])
            text = e.text or ""
            if not text or any(unicodedata.category(c).startswith('C') for c in text):
                return False
            if (y,x) <= previous:
                return False
            previous=(y,x)
            if status:
                if text != "● RUNNING  ^b ?" + " "*65:
                    return False
                statuses += 1
                continue
            if (x < 14 or (x - 14) % 9 or y < 12 or (y - 12) % 24
                    or y > 564 or x + 9 * len(text) > 734
                    or e.attrib.get("textLength") != str(9 * len(text))
                    or e.attrib.get("fill") not in {"#61afef","#abb2bf","#d4d4d4",
                        "#98c379","#e5c07b","#5c6370","#7cbff2"}):
                return False
            rows.setdefault(y, []).append((x,text,e.attrib["fill"],e.attrib.get("font-weight")))
        if statuses != 1 or rects != [
                {"width": "100%", "height": "100%", "fill": "#0c0c0c"},
                {"x": "0", "y": "588", "width": "748", "height": "24", "fill": "#1b1b1b"},
                {"x": "32", "y": "516", "width": "9", "height": "24", "fill": "none",
                 "stroke": "#d4d4d4", "stroke-width": "1"}]:
            return False
        for segments in rows.values():
            end = 14
            for x, s, _, _ in segments:
                if x < end:
                    return False
                end = x + len(s) * 9
        # The one observed completion aggregate; clipped command is eligibility
        # only. Final proof must show the FULL exact command independently.
        if type(command) is not str or not COMMAND_RE.fullmatch(command):
            return False
        aggregate = rows.get(396)
        if not aggregate or len(aggregate) != 7:
            return False
        count = re.fullmatch(r" ([2-9]) commands ", aggregate[3][1])
        if count is None:
            return False
        expected = [
            (14, "●", "#98c379", None), (23, " ", "#d4d4d4", None),
            (32, "Ran", "#e5c07b", "bold"), (59, " %s commands " % count.group(1), "#d4d4d4", None),
            (167, "(+%d · %s...)" % (int(count.group(1)) - 1, command[:20]), "#abb2bf", None),
            (437, " ", "#d4d4d4", None),
            (446, "(ctrl+o to expand)", "#abb2bf", None)]
        if aggregate != expected:
            return False
        # EXACT known dim mode placeholder + cursor, not arbitrary ghost text.
        if rows.get(516) != [(14, ">", "#7cbff2", None), (23, " ", "#d4d4d4", None),
                             (32, MODE, "#5c6370", None)]:
            return False
        for y in (492, 540):
            if rows.get(y) != [(14, BORDER, "#5c6370", None)]:
                return False
        # Observed quiet gap/completion caption: allow only the actual fixed
        # assistant caption, never use it as readiness or native proof alone.
        if rows.get(420) or rows.get(468):
            return False
        if rows.get(444) != [(14, "  " + FINAL_CAPTION, "#d4d4d4", None)]:
            return False
        if rows.get(564) != [(14, "? for shortcuts", "#5c6370", None),
                             (437, "auto", "#e5c07b", None),
                             (473, " · Gemini 4 Hydrogen · medium", "#d4d4d4", None)]:
            return False
        if set(rows) & {408, 432, 456, 480, 504, 528, 552}:
            return False
        return True
    except (ET.ParseError, KeyError, ValueError, TypeError):
        return False


def acquire(intent, *, read_identity, capture, validate_artifact, recognize_expanded,
            record_attempt, send, clock):
    """All callbacks injected. Every callback MUST enforce its supplied timeout.

    Synchronous Python cannot preempt a misbehaving callback. Late callback
    returns fail closed; hard wall-clock enforcement belongs to a future
    reviewed adapter. No such adapter or dispatch authority is included here.
    record_attempt MUST durably O_EXCL-fence this attempt before it returns.
    send receives exactly b'\x0f', once, and must NEVER retry internally.
    recognize_expanded is an injected candidate interface, NOT proof authority.
    """
    state = {"status": "UNKNOWN", "reason": "not_started", "key_attempted": False,
             "post_captures": 0, "frame_recognized": False, "native_authority": False}
    try:
        _valid_intent(intent)
        start = clock()
        if (type(start) not in (int, float) or type(intent.deadline) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(intent.deadline)):
            raise Refused("invalid_clock")
        deadline = min(intent.deadline, start + 10.0)
        last = start

        def now():
            nonlocal last
            value = clock()
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or value < last):
                raise Refused("invalid_clock")
            last = value
            return value

        def call(fn, *args):
            remaining = deadline - now()
            if remaining <= 0:
                raise Refused("deadline")
            value = fn(*args, deadline=deadline, timeout=remaining)
            if now() >= deadline:
                raise Refused("deadline")
            return value

        def identity():
            o = call(read_identity)
            if type(o) is not Observation:
                raise Refused("identity_or_ownership_changed")
            _valid_identity(o.identity)
            if (o.identity != intent.identity
                    or o.live is not True or o.current_assignment is not True
                    or o.known_human_intervention is not False):
                raise Refused("identity_or_ownership_changed")

        def snapshot():
            s = call(capture)
            if type(s) is not Snapshot:
                raise Refused("unbound_or_truncated_capture")
            _valid_identity(s.identity)
            if (s.identity != intent.identity
                    or s.complete is not True or type(s.svg) is not bytes
                    or len(s.svg) > 262144):
                raise Refused("unbound_or_truncated_capture")
            return s

        def artifact():
            digest = call(validate_artifact, intent)
            if type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise Refused("artifact_invalid")
            return digest

        def recognized(s):
            value = call(recognize_expanded, s, intent)
            if type(value) is not bool:
                raise Refused("recognizer_unknown")
            return value

        def finish():
            identity()
            if artifact() != original:
                raise Refused("artifact_changed")
            state.update(status="CANDIDATE_FRAME_RECOGNIZED", reason="independent_frame_callback",
                         frame_recognized=True)

        identity()
        original = artifact()
        before = snapshot()
        if recognized(before):
            finish()
            return state
        if not observed_collapsed(before.svg, intent.command):
            raise Refused("unsupported_or_nonidle_collapsed_view")
        identity()
        if artifact() != original:
            raise Refused("artifact_changed")
        # Mark local budget consumed BEFORE fencing/dispatch. Any error or lost
        # response terminates/reconciles without a second key invocation.
        state["key_attempted"] = True
        if call(record_attempt, intent, before, b"\x0f") is not True:
            raise Refused("attempt_fence_not_confirmed")
        identity()
        try:
            call(send, intent.identity, b"\x0f")
        except Refused:
            raise
        except Exception:
            state["reason"] = "send_outcome_unknown"
        for _ in range(2):
            identity()
            after = snapshot()
            state["post_captures"] += 1
            if recognized(after):
                finish()
                return state
        raise Refused("expanded_frame_unproved")
    except Refused as e:
        state["reason"] = str(e)
    except Exception:
        # No callback exception text/payload is exported.
        state["reason"] = "callback_failed"
    return state
