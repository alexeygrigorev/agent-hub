import os
import json
import uuid
import urllib.request
import urllib.parse
import urllib.error
from . import auth, config

def _do_request(method, path, payload=None):
    cfg = config.load_config()
    base_url = os.environ.get("HUB_URL") or os.environ.get("HUB_BASE_URL") or cfg.get("base_url")
    if not base_url:
        raise ValueError("Not logged in. Base URL not configured.")
    
    from .api import check_url, safe_http_json
    check_url(base_url)
    
    url = f"{base_url.rstrip('/')}{path}"
    headers = {"Content-Type": "application/json"}
    
    job_token = os.environ.get("HUB_JOB_TOKEN") or os.environ.get("HUB_TOKEN")
    if job_token:
        headers["Authorization"] = f"Bearer {job_token}"
    else:
        try:
            token = auth.get_access_token()
            headers["Authorization"] = f"Bearer {token}"
        except Exception:
            raise ValueError("No valid credentials found. Please login.")

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    else:
        data = None
        
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    
    try:
        return safe_http_json(req)
    except urllib.error.HTTPError as e:
        if e.code == 401 and not job_token:
            auth.refresh_token()
            headers["Authorization"] = f"Bearer {auth.get_access_token()}"
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            return safe_http_json(req)
        # Never print token, credentials, native full environment or server-controlled error bodies
        raise ValueError(f"HTTP request failed with status {e.code}")

def send(sender_actor, to_actor, kind, body, idempotency_key=None, thread_id=None, reply_to=None, lease_epoch=None):
    if idempotency_key is None:
        idempotency_key = str(uuid.uuid4())
    payload = {
        "from_actor": sender_actor,
        "to_actor": to_actor,
        "kind": kind,
        "body": body,
        "idempotency_key": idempotency_key
    }
    if thread_id:
        payload["thread_id"] = thread_id
    if reply_to:
        payload["reply_to"] = reply_to
    
    if lease_epoch is not None:
        payload["lease_epoch"] = int(lease_epoch)
    else:
        epoch = os.environ.get("HUB_LEASE_EPOCH")
        if epoch is not None and epoch != "":
            payload["lease_epoch"] = int(epoch)
        
    return _do_request("POST", "/api/bus/send", payload)

def inbox(actor, limit=50, cursor=None):
    encoded_actor = urllib.parse.quote(actor, safe="")
    path = f"/api/bus/inbox?actor={encoded_actor}&limit={limit}"
    if cursor:
        path += f"&cursor={urllib.parse.quote(cursor, safe='')}"
    return _do_request("GET", path)

def show(message_id):
    return _do_request("GET", f"/api/bus/messages/{urllib.parse.quote(message_id, safe='')}")

def _stage(message_id, action, lease_epoch=None):
    payload = None
    if lease_epoch is not None:
        payload = {"lease_epoch": int(lease_epoch)}
    else:
        epoch = os.environ.get("HUB_LEASE_EPOCH")
        if epoch is not None and epoch != "":
            payload = {"lease_epoch": int(epoch)}
    return _do_request("POST", f"/api/bus/messages/{urllib.parse.quote(message_id, safe='')}/{action}", payload)

def ack(message_id, lease_epoch=None):
    return _stage(message_id, "ack", lease_epoch=lease_epoch)

def accept(message_id, lease_epoch=None):
    return _stage(message_id, "accept", lease_epoch=lease_epoch)

def complete(message_id, lease_epoch=None):
    return _stage(message_id, "complete", lease_epoch=lease_epoch)

def reply(reply_to, sender_actor, to_actor, kind, body, idempotency_key=None, thread_id=None, lease_epoch=None):
    return send(sender_actor, to_actor, kind, body, idempotency_key=idempotency_key, thread_id=thread_id, reply_to=reply_to, lease_epoch=lease_epoch)

def claim(project, scope, holder, ttl_ms=3600000, lease_epoch=None):
    payload = {"project": project, "scope": scope, "holder": holder, "ttl_ms": ttl_ms}
    if lease_epoch is not None:
        payload["lease_epoch"] = int(lease_epoch)
    else:
        epoch = os.environ.get("HUB_LEASE_EPOCH")
        if epoch is not None and epoch != "":
            payload["lease_epoch"] = int(epoch)
    return _do_request("POST", "/api/bus/claims", payload)

def release(id_or_project_scope, lease_epoch=None):
    payload = None
    if lease_epoch is not None:
        payload = {"lease_epoch": int(lease_epoch)}
    else:
        epoch = os.environ.get("HUB_LEASE_EPOCH")
        if epoch is not None and epoch != "":
            payload = {"lease_epoch": int(epoch)}
    return _do_request("POST", f"/api/bus/claims/{urllib.parse.quote(id_or_project_scope, safe='')}/release", payload)

def list_claims(project=None, holder=None, active=True):
    path = "/api/bus/claims"
    params = []
    if project:
        params.append(f"project={urllib.parse.quote(project, safe='')}")
    if holder:
        params.append(f"holder={urllib.parse.quote(holder, safe='')}")
    if not active:
        params.append("active=false")
    if params:
        path += "?" + "&".join(params)
    return _do_request("GET", path)
