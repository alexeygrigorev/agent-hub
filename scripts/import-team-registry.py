#!/usr/bin/env python3
import sys
import json
import os
import urllib.request
import urllib.parse
import argparse

# Route contract: POST /api/agents/import
# Requires Bearer token with appropriate scope (admin/import)

def is_safe_url(url_str):
    if not isinstance(url_str, str):
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in url_str):
        return False
    try:
        parsed = urllib.parse.urlparse(url_str)
        # Reject userinfo (username / password / '@' in netloc)
        if parsed.username or parsed.password or "@" in parsed.netloc:
            return False
        if not parsed.hostname:
            return False
        # Port check
        if parsed.port is not None:
            if not (1 <= parsed.port <= 65535):
                return False
        if parsed.scheme == "https":
            return True
        if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"):
            return True
        return False
    except Exception:
        return False

# Real canonical keys recognized from TEAM-REGISTRY.json schema and specs
SENSITIVE_KEYS = {
    "token", "secret", "password", "private_key", "api_key",
    "access_token", "refresh_token", "credential", "credentials", "auth_token"
}

ALLOWED_TEAM_KEYS = {
    "id", "name", "head_tag", "principal_tags", "agents", "project", "host",
    "head_assignment_ack", "head_history", "head_session_id", "integration_handoff",
    "integration_owner", "members", "unassigned_roles", "updated_at"
}

ALLOWED_AGENT_KEYS = {
    "active_tasks", "actor_identity", "artifacts", "assignment_ack", "assignment_state",
    "boundaries", "branch", "cgroup_memory", "cgroup_pids", "commit_sha", "completed_at",
    "completed_commit", "containment", "conversation", "conversation_id",
    "count_as_active_worker", "current_timer_delegate", "custody_state", "deliverable",
    "delivered_artifact", "desktop_child_wake_restart", "engine", "evidence_level",
    "expected_checkpoint", "first_action", "first_tool", "first_tool_evidence",
    "full_native_identity", "harness_conversation_id", "head_session_id", "host",
    "human_owner_source", "implementation_workspace", "in_reply_to", "launch",
    "launched_at", "launched_by", "machine", "machine_id", "measurement_scope",
    "memory_cap", "memory_max", "mode", "model", "native_actor_ref", "native_agent_identity",
    "native_aplexer_identity", "native_identity", "next_checkpoint", "note",
    "observed_state", "opencode_session_id", "oversight_acceptance", "oversight_owner",
    "owned", "owned_files", "owned_paths", "ownership_handoff", "parent_chat_id",
    "parent_identity", "parent_identity_kind", "parent_native_identity", "parent_session",
    "parent_session_id", "parent_tag", "parent_team", "pid", "predecessor_identity",
    "principal_scope_ack", "private_recovery_note", "production_integration_owner",
    "project", "project_id", "provider", "provider_requested", "quota_evidence",
    "recovery_cause", "recovery_reason", "registered_at", "registered_by",
    "registration_scope_receipt", "remote_branch", "replaced_by", "replacement_session_id",
    "responsibility", "result", "resumed_at", "resumed_by", "resumed_conversation",
    "resumed_from_session_id", "resumed_store", "resumed_with_prompt", "resumed_with_task",
    "retired_at", "review_report", "role", "schedule", "scope", "scratch", "session_history",
    "session_id", "started_at", "state", "status", "successor_session_id", "supervision_excluded",
    "supervision_owner_session_id", "tag", "task", "task_id", "task_nature",
    "task_status", "tasks_max", "team", "team_id", "telemetry", "tree_sha", "updated_at",
    "usage", "usage_coverage", "usage_source", "verdict", "worker_pid", "workload_pid",
    "workspace"
}

ALLOWED_SESSION_HISTORY_KEYS = {
    "ended", "engine", "launch", "mode", "observation", "recorded_at",
    "registered_at", "registered_by", "session_id"
}

ALLOWED_FIRST_ACTION_KEYS = {
    "actual_tool_event_timestamp", "engine", "first_tool", "first_tool_at_utc",
    "handoff_read_at_utc", "kind", "observed_at_utc", "timestamp_verification",
    "whoami_at_utc"
}

ALLOWED_TELEMETRY_KEYS = {
    "conversation_id", "first_tool_id", "identity_evidence", "path", "scope", "type"
}

ALLOWED_HEAD_HISTORY_KEYS = {
    "agents", "at", "handoff_ack", "head_assignment_ack", "head_session_id", "head_tag", "integration_owner"
}

CANONICAL_ROLES = {
    "head", "implementer", "reviewer", "writer", "service", "principal", "root", "worker"
}

def canonicalize_role(raw_role):
    if not raw_role:
        return "worker"
    r = raw_role.strip().lower()
    if r in CANONICAL_ROLES:
        return r
    if "principal" in r:
        return "principal"
    if "root" in r:
        return "root"
    if "head" in r or "coordinator" in r:
        return "head"
    if any(k in r for k in ("writer", "scribe", "journal")):
        return "writer"
    if any(k in r for k in ("review", "checker", "audit", "verifier", "challenger", "diagnostician")):
        return "reviewer"
    if any(k in r for k in ("implement", "engineer", "executor", "repair", "reconciler", "automation")):
        return "implementer"
    if any(k in r for k in ("service", "daemon", "bridge", "helper")):
        return "service"
    if any(k in r for k in ("worker", "subagent", "researcher", "modeler", "adopter", "consumer", "observer", "architect")):
        return "worker"
    return None

def flatten_registry(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid registry format: root must be a dict")
    
    agents_map = {}
    seen_team_tags = set()
    seen_agents_tags = set()

    def validate_keys(d, allowed_keys, context_name):
        for k in d.keys():
            if k in SENSITIVE_KEYS or any(s in k.lower() for s in ("token", "secret", "password", "credential")):
                raise ValueError(f"Sensitive field rejected in {context_name}: {k}")
            if k not in allowed_keys:
                raise ValueError(f"Unknown field in {context_name}: {k}")

    def validate_agent_metadata(agent, context_name):
        validate_keys(agent, ALLOWED_AGENT_KEYS, context_name)
        if "session_history" in agent:
            sh = agent["session_history"]
            if sh is not None:
                if not isinstance(sh, list):
                    raise ValueError(f"Invalid session_history in {context_name}: must be a list")
                for item in sh:
                    if not isinstance(item, dict):
                        raise ValueError(f"Invalid session_history item in {context_name}: must be a dict")
                    validate_keys(item, ALLOWED_SESSION_HISTORY_KEYS, f"{context_name} session_history item")
        if "first_action" in agent:
            fa = agent["first_action"]
            if isinstance(fa, dict):
                validate_keys(fa, ALLOWED_FIRST_ACTION_KEYS, f"{context_name} first_action")
        if "telemetry" in agent:
            tel = agent["telemetry"]
            if isinstance(tel, dict):
                validate_keys(tel, ALLOWED_TELEMETRY_KEYS, f"{context_name} telemetry")

    def validate_team_metadata(team, context_name):
        validate_keys(team, ALLOWED_TEAM_KEYS, context_name)
        if "head_history" in team:
            hh = team["head_history"]
            if hh is not None:
                if not isinstance(hh, list):
                    raise ValueError(f"Invalid head_history in {context_name}: must be a list")
                for item in hh:
                    if not isinstance(item, dict):
                        raise ValueError(f"Invalid head_history item in {context_name}: must be a dict")
                    validate_keys(item, ALLOWED_HEAD_HISTORY_KEYS, f"{context_name} head_history item")
        if "members" in team:
            mems = team["members"]
            if mems is not None and isinstance(mems, list):
                for member in mems:
                    if isinstance(member, dict):
                        validate_agent_metadata(member, f"{context_name} member")

    teams = data.get("teams", [])
    if isinstance(teams, list):
        for team in teams:
            if not isinstance(team, dict):
                continue
            validate_team_metadata(team, "team")
            
            team_id = team.get("id")
            project = team.get("project")
            host = team.get("host")
            
            t_agents = team.get("agents", [])
            if isinstance(t_agents, list):
                for agent in t_agents:
                    if not isinstance(agent, dict):
                        continue
                    validate_agent_metadata(agent, "agent")
                    
                    raw_role = agent.get("role")
                    role = canonicalize_role(raw_role)
                    if raw_role and role is None:
                        raise ValueError(f"Unknown role: {raw_role}")
                    
                    tag = agent.get("tag") or agent.get("native_actor_ref") or agent.get("actor_identity")
                    if not tag or not isinstance(tag, str):
                        raise ValueError("Agent missing valid tag")
                    if tag in seen_team_tags:
                        raise ValueError(f"Duplicate tag ambiguity: {tag}")
                    seen_team_tags.add(tag)
                    
                    # Canonical agent metadata mapping:
                    # Ignore stale health/status/timestamps/session_history on import!
                    # Nested session_history must never leak into current binding.
                    is_completed = bool(agent.get("completed_at") or agent.get("retired_at"))
                    agents_map[tag] = {
                        "tag": tag,
                        "role": role or "worker",
                        "team_id": team_id or agent.get("team_id") or None,
                        "project": project or agent.get("project_id") or agent.get("project") or None,
                        "machine_id": host or agent.get("host") or agent.get("machine_id") or agent.get("machine") or None,
                        "workspace": agent.get("workspace") or None,
                        "session_id": agent.get("session_id") or None,
                        "engine": agent.get("engine") or agent.get("provider") or None,
                        "imported_from": "teams",
                        "_completed": is_completed
                    }

    agents_list = data.get("agents", [])
    if isinstance(agents_list, list):
        for agent in agents_list:
            if not isinstance(agent, dict):
                continue
            validate_agent_metadata(agent, "agent")
            
            raw_role = agent.get("role")
            role = canonicalize_role(raw_role)
            if raw_role and role is None:
                raise ValueError(f"Unknown role: {raw_role}")
            
            tag = agent.get("tag") or agent.get("native_actor_ref") or agent.get("actor_identity")
            if not tag or not isinstance(tag, str):
                raise ValueError("Agent missing valid tag")
            if tag in seen_agents_tags:
                raise ValueError(f"Duplicate tag ambiguity: {tag}")
            seen_agents_tags.add(tag)
            
            is_completed = bool(agent.get("completed_at") or agent.get("retired_at"))
            new_entry = {
                "tag": tag,
                "role": role or "worker",
                "team_id": agent.get("team_id") or None,
                "project": agent.get("project_id") or agent.get("project") or None,
                "machine_id": agent.get("host") or agent.get("machine_id") or agent.get("machine") or None,
                "workspace": agent.get("workspace") or None,
                "session_id": agent.get("session_id") or None,
                "engine": agent.get("engine") or agent.get("provider") or None,
                "imported_from": "agents",
                "_completed": is_completed
            }
            
            if tag in agents_map:
                existing = agents_map[tag]
                # Check for contradictory identities across lists
                if existing.get("project") and new_entry.get("project") and existing["project"] != new_entry["project"]:
                    raise ValueError(f"Duplicate tag ambiguity: {tag}")
                if existing.get("machine_id") and new_entry.get("machine_id") and existing["machine_id"] != new_entry["machine_id"]:
                    raise ValueError(f"Duplicate tag ambiguity: {tag}")

                # Real current session/workspace/machine tuple deterministic ambiguity policy:
                if existing.get("session_id") and new_entry.get("session_id"):
                    if existing["session_id"] != new_entry["session_id"]:
                        raise ValueError(f"Duplicate tag ambiguity: {tag}")
                    if existing.get("workspace") and new_entry.get("workspace") and existing["workspace"] != new_entry["workspace"]:
                        raise ValueError(f"Duplicate tag ambiguity: {tag}")
                    if existing.get("machine_id") and new_entry.get("machine_id") and existing["machine_id"] != new_entry["machine_id"]:
                        raise ValueError(f"Duplicate tag ambiguity: {tag}")
                elif existing.get("session_id") is None and new_entry.get("session_id") is not None:
                    # new_entry has the bound session tuple
                    existing["session_id"] = new_entry["session_id"]
                    if new_entry.get("workspace") is not None:
                        existing["workspace"] = new_entry["workspace"]
                    if new_entry.get("machine_id") is not None:
                        existing["machine_id"] = new_entry["machine_id"]
                elif existing.get("session_id") is not None and new_entry.get("session_id") is None:
                    # existing already has the bound session tuple, preserve it
                    pass
                else:
                    # Both unbound
                    if existing.get("workspace") and new_entry.get("workspace") and existing["workspace"] != new_entry["workspace"]:
                        if existing["_completed"] and not new_entry["_completed"]:
                            existing["workspace"] = new_entry["workspace"]
                        elif new_entry["_completed"] and not existing["_completed"]:
                            pass
                        else:
                            raise ValueError(f"Duplicate tag ambiguity: {tag}")

                for k, v in new_entry.items():
                    if k != "_completed" and v is not None and existing.get(k) is None:
                        existing[k] = v
            else:
                agents_map[tag] = new_entry

    if len(agents_map) > 1000:
        raise ValueError("Too many agents: exceeds 1000")

    for a in agents_map.values():
        a.pop("_completed", None)

    return list(agents_map.values())

class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"Redirect to {newurl} refused", headers, fp)

def do_import(agents, url, token):
    if not is_safe_url(url):
        print("Error: Unsafe URL. Must be HTTPS or localhost HTTP without userinfo.", file=sys.stderr)
        sys.exit(1)

    opener = urllib.request.build_opener(NoRedirectHandler())
    urllib.request.install_opener(opener)

    req = urllib.request.Request(url, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    
    data = json.dumps({"agents": agents}).encode("utf-8")
    if len(data) > 1024 * 1024:
        raise ValueError("Payload too large: exceeds 1MB")
        
    try:
        with urllib.request.urlopen(req, data=data, timeout=10) as resp:
            # Bounded read to avoid memory exhaustion
            resp_body = resp.read(10 * 1024 * 1024 + 1)
            if len(resp_body) > 10 * 1024 * 1024:
                print("Error: Response too large", file=sys.stderr)
                sys.exit(1)
            if resp.status != 200:
                print(f"Server error: {resp.status} {resp_body.decode('utf-8', errors='ignore')[:1024]}", file=sys.stderr)
                sys.exit(1)
            print("Import successful.")
    except urllib.error.HTTPError as e:
        body = e.read(65536).decode('utf-8', errors='ignore')
        print(f"Server error: {e.code} {body[:1024]}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Network error: {e}", file=sys.stderr)
        sys.exit(1)

def get_token():
    token = os.environ.get("HUB_IMPORT_TOKEN")
    if token:
        return token
    config_path = os.path.expanduser("~/.config/agent-hub/import.token")
    if os.path.exists(config_path):
        stat = os.stat(config_path)
        if stat.st_mode & 0o077:
            print(f"Warning: {config_path} has open permissions. Should be 600.", file=sys.stderr)
            sys.exit(1)
        with open(config_path, "r") as f:
            return f.read().strip()
    return None

def main():
    parser = argparse.ArgumentParser(description="Import TEAM-REGISTRY to Hub")
    parser.add_argument("file", help="Path to TEAM-REGISTRY.json")
    parser.add_argument("--apply", action="store_true", help="Apply changes via API (default: dry run)")
    parser.add_argument("--url", help="Hub API URL (e.g. http://localhost:8787/api/agents/import)")
    args = parser.parse_args()
    
    if args.apply:
        if not args.url:
            print("Error: --url required for --apply", file=sys.stderr)
            sys.exit(1)
        if not is_safe_url(args.url):
            print("Error: Unsafe URL. Must be HTTPS or localhost HTTP without userinfo.", file=sys.stderr)
            sys.exit(1)
            
    file_stat = os.stat(args.file)
    if file_stat.st_size > 5 * 1024 * 1024:
        print("File too large", file=sys.stderr)
        sys.exit(1)
        
    with open(args.file, "r") as f:
        data = json.load(f)
        
    try:
        agents = flatten_registry(data)
    except ValueError as e:
        print(f"Validation error: {e}", file=sys.stderr)
        sys.exit(1)
    
    print(f"Parsed {len(agents)} agents.")
    for a in agents:
        print(f" - {a['tag']} ({a['role']})")
        
    if args.apply:
        token = get_token()
        if not token:
            print("Error: HUB_IMPORT_TOKEN environment variable or config file required", file=sys.stderr)
            sys.exit(1)
            
        do_import(agents, args.url, token)
    else:
        print("\nDry-run complete. Use --apply to send to Hub.")

if __name__ == "__main__":
    main()
