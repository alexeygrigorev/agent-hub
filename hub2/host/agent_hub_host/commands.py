"""Typed compatibility commands over the same bounded public API."""
import json
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode


ACTIONS = {
    "repos": "list show edit", "intake": "create list show history context append update assign depend blocker forgotten",
    "sessions": "start list show send commands capture stop wake count tree",
    "msg": "send inbox show reply ack status readdress", "quota": "show banked",
    "auth": "status login refresh logout", "schedules": "list create pause resume", "driver": "start status",
}


def add_commands(subparsers):
    for group, actions in ACTIONS.items():
        parser = subparsers.add_parser(group)
        parser.add_argument("action", choices=actions.split(), nargs="?", default=actions.split()[0])
        parser.add_argument("identity", nargs="?")
        for flag in ("project", "status", "after", "cursor", "search", "title", "text", "text-file", "acceptance-criteria-file",
                     "intake-task-id", "machine", "machine-id", "key", "body-file", "prompt", "file", "to",
                     "body", "mailbox", "thread", "state", "workspace", "tier", "engine", "model", "effort", "reason", "at"):
            parser.add_argument("--" + flag)
        parser.add_argument("--acceptance-criteria", action="append", default=[])
        parser.add_argument("--required-capability", action="append", default=[])
        parser.add_argument("--limit", type=int, default=25)
        parser.add_argument("--epoch", type=int)
        for flag in ("json", "all", "interactive", "with-goal", "no-browser"):
            parser.add_argument("--" + flag, action="store_true")
    subparsers.add_parser("status")
    subparsers.add_parser("model-policy")


def file_text(path):
    value = Path(path).read_text(encoding="utf-8")
    if len(value.encode()) > 16384:
        raise ValueError("input file too large")
    return value


def execute(client, args, config):
    group, action = args.command, getattr(args, "action", "show")
    identity = quote(args.identity, safe="") if getattr(args, "identity", None) else None
    query = {}
    key = getattr(args, "key", None) or "cli-" + str(uuid.uuid4())
    body = json.loads(file_text(args.file)) if getattr(args, "file", None) else {}
    if not isinstance(body, dict):
        raise ValueError("JSON object required")
    text = file_text(args.text_file) if getattr(args, "text_file", None) else getattr(args, "text", None)
    prompt = file_text(args.body_file) if getattr(args, "body_file", None) else getattr(args, "prompt", None)
    method, path = "GET", "/api/" + group
    if group == "repos":
        path = "/api/repos" + ("/" + identity if identity else "")
        if action == "edit":
            if not identity or not args.file:
                raise ValueError("repo and --file required")
            method = "PATCH"
        elif action == "show" and not identity:
            raise ValueError("repo required")
        elif action == "list":
            query["status"] = args.status or "active"
            if args.after:
                query["after"] = args.after
    elif group == "intake":
        query["project"] = args.project or config.get("project")
        if not query["project"]:
            raise ValueError("--project required")
        path = "/api/intakes" + ("/" + identity if identity else "")
        if action=='list':
            query.update({key:value for key,value in {'status':args.status,'search':args.search}.items() if value})
        if action == "create":
            if not args.title or not text:
                raise ValueError("title and text required")
            criteria = args.acceptance_criteria
            if args.acceptance_criteria_file:
                raw = file_text(args.acceptance_criteria_file)
                criteria += json.loads(raw) if raw.lstrip().startswith("[") else raw.splitlines()
            body.update(title=args.title, text=text, acceptance_criteria=criteria, key=key)
            method = "POST"
        elif action == "update":
            method = "PATCH"
            body.update({k: v for k, v in {"status": args.status, "title": args.title, "text": text}.items() if v is not None})
        elif action not in {"list", "show"}:
            path += "/" + action
            if action not in {"history", "context", "forgotten"}:
                method = "POST"
                body.update(key=key)
                if text or prompt:
                    body["text"] = text or prompt
    elif group == "sessions":
        if action == "start":
            if not args.intake_task_id or not args.project:
                raise ValueError("--intake-task-id and --project required")
            path = "/api/intakes/" + quote(args.intake_task_id, safe="") + "/start"
            query["project"] = args.project
            body = {"key": key,"interactive":args.interactive or args.with_goal,"with_goal":args.with_goal,
                    "required_capabilities":args.required_capability}
            if args.machine_id or args.machine:
                body["machine_id"]=args.machine_id or args.machine
            if args.model:
                body["model"]=args.model
            if args.effort:
                body['effort']=args.effort
            if args.reason:
                body["override_reason"]=args.reason
            if args.workspace or args.engine or prompt:
                body.update(workspace=args.workspace, engine=args.engine, interactive=args.interactive or args.with_goal,
                            with_goal=args.with_goal, tier=args.tier)
                if prompt is not None:
                    body["prompt"] = prompt
            method = "POST"
        else:
            path = "/api/sessions" + ("/" + identity if identity else "")
            if args.machine_id or args.machine:
                query["machine_id"] = args.machine_id or args.machine
            if action in {"send", "capture", "stop"}:
                if not identity:
                    raise ValueError("session required")
                path += "/commands"
                method = "POST"
                body = {"kind": {"send": "send", "capture": "capture", "stop": "stop"}[action], "key": key}
                if prompt is not None:
                    body["text"] = prompt
            elif action not in {"list", "show"}:
                path += "/" + action
                if action == "wake":
                    method, body = "POST", {"at": args.at, "prompt": prompt, "key": key}
    elif group == "msg":
        path = "/api/messages" + ("/" + identity if identity else "")
        if args.mailbox:
            query["mailbox"] = args.mailbox
        if action in {"send", "reply"}:
            method, path = "POST", "/api/messages"
            content = file_text(args.body_file) if args.body_file else args.body
            if action == "reply":
                original = client.request("GET", "/api/messages/" + (identity or "") + ("?" + urlencode(query) if query else ""))["message"]
                body.update(reply_to=args.identity, parent_sender=original["sender"])
                recipient = args.to or original["sender"]
            else:
                recipient = args.to
            if not recipient or not content:
                raise ValueError("recipient and body required")
            body.update(recipient=recipient, body=content, key=key)
        elif action == "inbox":
            query.update({k: v for k, v in {"thread": args.thread, "state": args.state, "after": args.after}.items() if v})
        elif action not in {"show", "status"}:
            path += "/" + action
            epoch = args.epoch
            if epoch is None:
                original = client.request("GET", "/api/messages/" + (identity or "") + ("?" + urlencode(query) if query else ""))["message"]
                epoch = original["epoch"]
            method, body = "POST", {**body, "epoch": epoch}
            if action == "readdress":
                body.update(recipient=args.to, key=key)
        elif action == "status":
            path += "/status"
    elif group == "quota":
        path = "/api/account/quota" if action == "show" else "/api/account/resets"
    elif group == "auth":
        from .auth import authenticate
        return authenticate(client,args,config)
    elif group in {"schedules", "driver"} and action not in {"list", "status"}:
        path += ("/" + identity if identity else "") + "/" + action
        method = "POST"
    if getattr(args, "limit", None):
        if not 1 <= args.limit <= 100:
            raise ValueError("limit must be 1..100")
        if method == "GET" and group not in {"auth", "quota"}:
            query["limit"] = args.limit
    base_path = path
    page_key='cursor' if group=='intake' and action=='list' else 'after'
    if getattr(args,'cursor',None) and (page_key!='cursor' or getattr(args,'after',None)):
        raise ValueError('--cursor is only for intake list and cannot combine with --after')
    if method=='GET' and (getattr(args,'cursor',None) or getattr(args,'after',None)):
        query[page_key]=getattr(args,'cursor',None) or getattr(args,'after',None)
    if query:
        path += "?" + urlencode(query)
    result = client.request(method, path, None if method == "GET" else body, key if method != "GET" else None)
    seen = set()
    while getattr(args, "all", False) and method == "GET" and result.get("next"):
        cursor = str(result["next"])
        if cursor in seen or len(seen) >= 100:
            raise ValueError("pagination did not finish within bounds")
        seen.add(cursor)
        query[page_key] = cursor
        page = client.request("GET", base_path + "?" + urlencode(query))
        for name, value in page.items():
            if isinstance(value, list):
                result.setdefault(name, []).extend(value)
        result["next"] = page.get("next")
    if group == "msg" and action == "show":
        epoch = result["message"]["epoch"]
        client.request("POST", "/api/messages/" + identity + "/read" + ("?" + urlencode(query) if query else ""), {"epoch": epoch}, key)
    return result
