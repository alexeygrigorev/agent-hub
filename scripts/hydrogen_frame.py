"""Hydrogen expanded native-Bash frame recognizer (pure; no I/O, no commands).

Integrated from Root's private recognizer proposal V2 (source sha256
8a48dbc0c8f01a4d680651de3123b9b1b99d1f9c3b57bf1e633e6153e8e6a171). The SVG
grammar, styles, geometry, timestamp, ambiguity and binding checks are kept
verbatim. Integration differences, all documented in docs/HUB-ACCEPTANCE-10.md:

* The probe path identifier is ``sha256(C([run_id, sample]))[:12]`` instead of
  also hashing the child intake id: the formal driver freezes the child brief
  (which must name the command) BEFORE the child intake id exists. ``run_id`` is
  a fresh uuid4 per run, so (run, sample) is still unique; the child intake id
  stays bound through the canonical artifact and the pinned helper binding.
* The rendered title's ``~`` is derived from an explicit Root-supplied home
  directory instead of a hard-coded user home.
* The optional private-file reader is omitted: the driver passes the exact
  bytes it collected through its bounded native collector.

A match is a candidate renderer/artifact association only. Acquisition,
freshness, custody and identity are separate driver obligations.
"""
import ast
import datetime
import hashlib
import json
import re
import shlex
import unicodedata
import uuid
import xml.etree.ElementTree as ET

MAX_SVG = 65536
NS = '{http://www.w3.org/2000/svg}'
KEYS = set('schema run_id sample parent_intake_id child_intake_id session_id native_id project machine_id workspace head tracked_status_sha256 tracked_status_entries calculation'.split())
_DISCLAIMER = re.compile(r'\b(?:quoted\s+example|example\s+above|not\s+(?:(?:an?|actually)\s+)?executed(?:\s+command)?|illustrative\s+example|simulated\s+(?:command|output))\b', re.IGNORECASE)


class Refused(ValueError):
    pass


def need(condition):
    if not condition:
        raise Refused('candidate expanded-frame mismatch')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def h7_rel_path(run_id, sample):
    """The one short exact probe path of the reviewed grammar `.local/h7_<12hex>_<NN>.py`."""
    need(type(run_id) is str and str(uuid.UUID(run_id)) == run_id)
    need(type(sample) is int and 1 <= sample <= 10)
    ident = hashlib.sha256(canonical([run_id, sample])).hexdigest()[:12]
    return '.local/h7_%s_%02d.py' % (ident, sample)


def expected_title(workspace, session_id, home):
    """`aplexer ~/<workspace below home>:task-hub2-<session>` as rendered by the native client."""
    need(type(home) is str and home.startswith('/') and not home.endswith('/') and '//' not in home)
    need(type(workspace) is str and workspace.startswith(home + '/'))
    return 'aplexer ~/' + workspace[len(home) + 1:] + ':task-hub2-' + session_id


def strict_json(raw):
    need(type(raw) is bytes and len(raw) <= 8192)
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result)
            result[key] = value
        return result
    def bad(_):
        raise Refused('nonfinite JSON')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=bad, parse_float=bad)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise Refused('candidate JSON refused') from error


def safe_work(work):
    need(type(work) is dict and set(work) == KEYS)
    for key in ('run_id', 'parent_intake_id', 'child_intake_id', 'session_id', 'native_id'):
        value = work[key]
        need(type(value) is str and str(uuid.UUID(value)) == value)
    need(work['schema'] == 'phase7_native_work_v2')
    need(work['project'] == 'alexeygrigorev/agent-hub')
    need(work['machine_id'] == 'hetzner-aplexer')
    need(type(work['sample']) is int and 1 <= work['sample'] <= 10)
    need(type(work['calculation']) is int and work['calculation'] == 5050)
    need(type(work['tracked_status_entries']) is int and 0 <= work['tracked_status_entries'] <= 65536)
    need(type(work['workspace']) is str and work['workspace'].startswith('/'))
    need('..' not in work['workspace'].split('/') and '//' not in work['workspace'])
    for key, count in [('head', 40), ('tracked_status_sha256', 64)]:
        need(type(work[key]) is str and re.fullmatch('[0-9a-f]{' + str(count) + '}', work[key]) is not None)
    for value in work.values():
        if isinstance(value, str):
            need(len(value.encode()) <= 4096 and not any(unicodedata.category(c).startswith('C') for c in value))


def rows_from_svg(raw, title):
    need(type(raw) is bytes and 0 < len(raw) <= MAX_SVG)
    need(b'<!' not in raw and b'<?' not in raw)  # no DTD/entity/comment/processing instruction
    try:
        root = ET.fromstring(raw.decode('utf-8'))
    except (ET.ParseError, UnicodeError) as error:
        raise Refused('candidate SVG refused') from error
    # Deliberately one observed renderer size/schema. Other formats remain UNKNOWN.
    need(root.tag == NS + 'svg' and root.attrib == {
        'width': '748', 'height': '624', 'viewBox': '0 0 748 624',
        'font-family': 'DejaVu Sans Mono,Menlo,Consolas,monospace'})
    need(len(root) == 3 and root[0].tag == NS + 'title' and root[0].attrib == {})
    need(type(title) is str and root[0].text == title and len(root[0]) == 0)
    need(root[1].tag == NS + 'rect' and root[1].attrib == {'width': '100%', 'height': '100%', 'fill': '#0c0c0c'} and len(root[1]) == 0)
    group = root[2]
    need(group.tag == NS + 'g' and group.attrib == {'font-size': '15', '{http://www.w3.org/XML/1998/namespace}space': 'preserve'})
    need(0 < len(group) <= 512)
    for parent in [root, group]:
        need(not parent.text or parent.text.isspace())
        for child in parent:
            need(not child.tail or child.tail.isspace())
    rows = {}
    decorations = []
    previous = (-1, -1)
    for item in group:
        if item.tag == NS + 'rect':
            need(not len(item) and not item.text)
            decorations.append(item.attrib)
            continue
        need(item.tag == NS + 'text' and not len(item))
        status = item.attrib == {'x': '14', 'y': '605', 'fill': '#c8cdd4'}
        if status:
            need(item.text == '● RUNNING  ^b ?' + ' ' * 65)
        else:
            need(set(item.attrib) in ({'x', 'y', 'fill', 'textLength'}, {'x', 'y', 'fill', 'textLength', 'font-weight'}))
        if 'font-weight' in item.attrib:
            need(item.attrib['font-weight'] == 'bold')
        text = item.text or ''
        need(not any(unicodedata.category(c).startswith('C') for c in text))
        need(all(re.fullmatch(r'0|[1-9][0-9]{0,3}', item.attrib[k]) for k in ('x', 'y')))
        if status:
            x, y, length = 14, 605, 9 * len(text)
        else:
            need(re.fullmatch(r'0|[1-9][0-9]{0,3}', item.attrib['textLength']) is not None)
            x, y, length = (int(item.attrib[k]) for k in ('x', 'y', 'textLength'))
        need(0 <= x < 748 and 0 < y <= 624 and x + length <= 748)
        need(length == 9 * len(text))
        need(re.fullmatch('#[0-9a-f]{6}', item.attrib['fill']) is not None)
        need((y, x) > previous)
        previous = (y, x)
        row = rows.setdefault(y, [])
        need(not row or row[-1][0] + 9 * len(row[-1][3]) <= x)
        row.append((x, item.attrib['fill'], item.attrib.get('font-weight'), text))
    need(decorations == [
        {'x': '0', 'y': '588', 'width': '748', 'height': '24', 'fill': '#1b1b1b'},
        {'x': '32', 'y': '516', 'width': '9', 'height': '24', 'fill': 'none', 'stroke': '#d4d4d4', 'stroke-width': '1'}])
    return rows


def timestamp(text, prefix):
    need(text.startswith(prefix))
    value = text[len(prefix):]
    need(re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-](?:[01]\d|2[0-3]):[0-5]\d', value) is not None)
    try:
        parsed = datetime.datetime.fromisoformat(value)
        need(parsed.isoformat(timespec='seconds') == value)
        return parsed
    except ValueError as error:
        raise Refused('candidate timestamp refused') from error


_UNRELATED_TOKENS = ('AC10', '…', '...', 'ctrl+o')


def _probe_preparation(command, script_relpath):
    """Recognize only visible literal copy preparation, never arbitrary Python.

    This classifies context; it cannot create work proof or execute anything.
    Other h7 references stay ambiguous. Shell expansion/operators cannot hide
    an invocation inside an otherwise innocent-looking copy command.
    """
    if '$' in command or '`' in command:
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>()')
        lexer.whitespace_split = True
        lexer.commenters = ''
        argv = list(lexer)
        if len(argv) == 3 and argv[0] == 'cp':
            return (argv[2] == script_relpath and bool(argv[1])
                    and not any(c in argv[1] for c in ';&|<>()'))
        if not argv or argv[0] != 'python3':
            return False
        if len(argv) == 3 and argv[1] == '-c':
            code = argv[2]
        elif len(argv) == 2 and argv[1].startswith('-c'):
            code = argv[1][2:]
        else:
            return False
        body = ast.parse(code, mode='exec').body
        if len(body) != 2 or not isinstance(body[1], ast.Expr):
            return False
        imported, expression = body[0], body[1].value
        if not isinstance(expression, ast.Call) or expression.keywords or len(expression.args) != 2:
            return False
        if not all(isinstance(arg, ast.Constant) and type(arg.value) is str and arg.value
                   for arg in expression.args) or expression.args[1].value != script_relpath:
            return False
        if isinstance(imported, ast.Import):
            return (len(imported.names) == 1 and imported.names[0].name == 'shutil'
                    and imported.names[0].asname is None
                    and isinstance(expression.func, ast.Attribute) and expression.func.attr == 'copy'
                    and isinstance(expression.func.value, ast.Name) and expression.func.value.id == 'shutil')
        return (isinstance(imported, ast.ImportFrom) and imported.module == 'shutil' and imported.level == 0
                and len(imported.names) == 1 and imported.names[0].name in ('copy', '*')
                and imported.names[0].asname is None
                and isinstance(expression.func, ast.Name) and expression.func.id == 'copy')
    except (ValueError, SyntaxError):
        return False


def _unrelated_header(row, script_relpath):
    """True only for a complete native tool header whose command cannot be the probe."""
    if len(row) != 4:
        return False
    marker, gap, tool, args = row
    if gap[:2] != (23, '#d4d4d4') or gap[3] != ' ' or tool[0] != 32 or tool[2] != 'bold':
        return False
    if re.fullmatch('[A-Za-z]{1,32}', tool[3]) is None or args[0] != 32 + 9 * len(tool[3]):
        return False
    text = args[3]
    if not (text.startswith('(') and text.endswith(')') and 2 < len(text) <= 80):
        return False
    if any(token in text for token in _UNRELATED_TOKENS):
        return False
    command = text[1:-1]
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>()')
        lexer.whitespace_split = True
        lexer.commenters = ''
        argv = list(lexer)
    except ValueError:
        return False
    # Shell quoting/backslash escapes must not conceal a second h7 invocation.
    probe_reference = 'h7_' in command.lower() or any('h7_' in arg.lower() for arg in argv)
    if not probe_reference and '.py' not in text:
        return True
    if (not probe_reference and tool[3] == 'Bash' and len(argv) == 2
            and argv[0] == 'python3' and argv[1].endswith('.py')
            and not argv[1].startswith('-')
            and not any(c in argv[1] for c in '*?[]{}')
            and not any(c in command for c in '$`')):
        return True  # a complete literal invocation of another Python script
    # Only a positively completed Bash copy is a known preparation reference.
    # Unknown Python/shell expressions mentioning scripts remain ambiguous.
    return (marker == (14, '#98c379', None, '●') and tool[3] == 'Bash'
            and _probe_preparation(command, script_relpath))


def recognize(svg, *, work, script_relpath, script_sha256, pinned_script,
              challenge, canonical_artifact, expected_command, home):
    """All arguments are independent Root-frozen byte snapshots, NOT values read from the SVG.

    A match is only a candidate renderer/artifact association. No file or command I/O.
    """
    safe_work(work)
    need(type(pinned_script) is bytes and 0 < len(pinned_script) <= 65536)
    need(type(script_sha256) is str and re.fullmatch('[0-9a-f]{64}', script_sha256) is not None)
    need(hashlib.sha256(pinned_script).hexdigest() == script_sha256)
    need(script_relpath == h7_rel_path(work['run_id'], work['sample']))
    need(expected_command == 'python3 ' + script_relpath)
    need(type(challenge) is bytes and len(challenge) == 32)
    digest = hashlib.sha256(b'HUB_PHASE7_NATIVE_WORK_V2\0' + challenge + b'\0' + canonical(work)).hexdigest()
    artifact = strict_json(canonical_artifact)
    need(type(artifact) is dict and set(artifact) == KEYS | {'proof_digest'})
    need(canonical_artifact == canonical({**work, 'proof_digest': digest}) + b'\n')
    title = expected_title(work['workspace'], work['session_id'], home)
    rows = rows_from_svg(svg, title)
    # Exact target selection. Every native-looking tool header is a target
    # candidate unless it is a complete, well-formed header for a provably
    # unrelated command or literal helper copy preparation. An actual probe
    # invocation, unknown probe reference, evidence token, truncation or
    # malformed/bare marker fails closed as a candidate. Exactly
    # one candidate may exist, so a duplicate, same-command running/failed,
    # another sample's probe or a truncated row is ambiguous; no "best block".
    native = []
    for y, row in rows.items():
        if row and row[0][0] == 14 and row[0][3] in ('●', '○') and not _unrelated_header(row, script_relpath):
            native.append((y, row))
    need(len(native) == 1)
    y, header = native[0]
    need(header == [(14, '#98c379', None, '●'), (23, '#d4d4d4', None, ' '),
                    (32, '#e5c07b', 'bold', 'Bash'), (68, '#abb2bf', None, '(' + expected_command + ')')])
    need(y + 192 <= 492)  # proof cannot overlap observed prompt cursor/status decorations
    created = rows.get(y + 24)
    completed = rows.get(y + 48)
    need(created is not None and len(created) == 2 and created[0] == (14, '#abb2bf', None, '  ⎿  '))
    need(created[1][:3] == (59, '#d4d4d4', None))
    need(completed is not None and len(completed) == 1 and completed[0][:3] == (59, '#d4d4d4', None))
    start = timestamp(created[1][3], 'Created At: ')
    end = timestamp(completed[0][3], 'Completed At: ')
    need(start <= end)
    expected_rows = {
        y + 72: [(59, '#d4d4d4', None, 'Command exited with code 0.')],
        y + 120: [(59, '#d4d4d4', None, 'Command output:')],
        y + 144: [(59, '#d4d4d4', None, 'AC10_WORK_V2 %s %d' % (work['run_id'], work['sample']))],
        y + 168: [(59, '#d4d4d4', None, digest)],
        y + 192: [(59, '#d4d4d4', None, work['head'] + ' 5050 '),
                  (473, '#abb2bf', None, '(ctrl+o to collapse)')],
    }
    for line, spans in expected_rows.items():
        need(rows.get(line) == spans)
    need(y + 96 not in rows)  # observed blank separator, no concealed failed/running row
    allowed = {y, y + 24, y + 48, *expected_rows}
    need(all(not y <= line <= y + 192 or line in allowed for line in rows))
    # No second copy of canonical work evidence before/after this block (prose,
    # echoed input, replay, truncation, or an unrelated footer cannot be proof).
    outside = []
    for line, spans in rows.items():
        if not y <= line <= y + 192:
            outside.append(''.join(s[3] for s in spans))
        if line in (y + 144, y + 168, y + 192):
            continue
        text = ''.join(s[3] for s in spans)
        need(not any(token in text for token in ('AC10_WORK_V2', digest, work['head'] + ' 5050')))
        if line != y:
            # A second exact command anywhere outside the native header is ambiguous.
            need(expected_command not in text)
            need(_DISCLAIMER.search(text) is None)
    # Refusal-only context over rows of THIS one SVG; it can never create proof.
    context = ' '.join(outside)
    need(re.sub(r'\s+', '', expected_command) not in re.sub(r'\s+', '', context))
    need(_DISCLAIMER.search(context) is None)
    return {'status': 'CANDIDATE_EXPANDED_FRAME_MATCH',
            'svg_sha256': hashlib.sha256(svg).hexdigest(),
            'artifact_sha256': hashlib.sha256(canonical_artifact).hexdigest(),
            'script_sha256': script_sha256, 'proof_digest': digest,
            'created_at': start.isoformat(), 'completed_at': end.isoformat()}
