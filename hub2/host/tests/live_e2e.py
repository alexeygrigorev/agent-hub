"""Private live dev proof. Never points at production or invents provider quota."""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hub2_agent_hub_host.client import ApiError, Client, private_file, save_config


def write_private(path, value):
    save_config(path, value)


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def run(args):
    repo = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_file(output)
    cfg = json.loads(args.operator_config.read_text())
    server = cfg["server"]
    require(server.startswith(("http://127.0.0.1:", "http://localhost:", "https://agent-hub2-")), "isolated_dev_server_required")
    require("hub.dtcdev.click" not in server, "production_refused")
    operator = Client(server, cfg["token"])
    suffix = uuid.uuid4().hex[:12]
    project, machine = "example/e2e-" + suffix, "e2e-host-" + suffix
    workspace = output / "native-workspace"
    workspace.mkdir(mode=0o700)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    hello = workspace / "HELLO.txt"
    require(not hello.exists(), "preexisting_artifact_refused")
    record = {"server_kind": "local-workerd" if server.startswith("http:") else "remote-development",
              "project": project, "machine_id": machine, "auth": {}, "stages": []}
    agent = None
    try:
        try:
            Client(server).request("GET", "/api/repos")
            raise RuntimeError("unauthenticated_request_was_accepted")
        except ApiError as error:
            require(str(error) == "Hub HTTP 401", "unexpected_auth_refusal")
        record["auth"]["unauthenticated_refused"] = True
        operator.request("PATCH", "/api/repos/" + quote(project, safe=""), {
            "canonical_url": "https://github.com/" + project, "purpose": "isolated live Hub2 verification",
            "status": "active", "domains": [], "aliases": []})
        record["stages"].append("canonical_repo_registered")
        operator.request("PATCH", "/api/account", {"provider": "go", "enabled": True, "capacity": 1, "min_remaining": 0})
        # This is an existing private native export, imported only to the isolated encrypted service.
        export = args.go_device_export.read_text()
        require(isinstance(json.loads(export), list), "active_go_device_array_required")
        operator.request("PUT", "/api/account/credentials", {
            "expected_generation": 0, "files": {".local/share/opencode-hub/device-auth.json": export}})
        operator.request("POST", "/api/account/discovery", {})
        quota = operator.request("GET", "/api/account/quota")
        write_private(output / "actual-provider-quota.json", quota)
        require(quota.get("fresh") is True and quota.get("quota", {}).get("remaining", 0) > 0
                and str(quota["quota"].get("source", "")).startswith("provider:go:"), "genuine_provider_quota_unavailable")
        record["stages"].append("actual_go_provider_quota_collected")
        pairing = operator.request("POST", "/api/enrollments/pair", {"machine_id": machine, "alias": machine})
        enrolled = Client(server).request("POST", "/api/enrollments/complete", {"code": pairing["pairing_code"]})
        operator.request("PUT", "/api/account/hosts", {"hosts": [machine]})
        launcher = json.loads(args.launcher_config.read_text())
        machine_cfg = {"server": server, "token": enrolled["token"], "machine_id": machine,
                       "roots": [str(workspace)], "engines": ["opencode"], "launcher": launcher}
        machine_path = output / "machine.json"
        write_private(machine_path, machine_cfg)
        host = Client(server, enrolled["token"])
        host.heartbeat(["e2e", "engine:opencode"])
        record["stages"].append("minted_machine_enrollment_and_heartbeat")
        prompt = (f'Work only in {workspace}. Write {hello} with exact UTF-8 bytes represented by JSON string "Hub2 guarded dev E2E passed" (no period, newline, or other bytes). '
                  "Do not edit other source or contact external services. Complete launcher first-action proof and exit.")
        intake = operator.request("POST", "/api/intakes?" + urlencode({"project": project}), {
            "title": "Guarded native dev E2E", "text": prompt,
            "acceptance_criteria": ["Genuine maintained native launch", "Exact synthetic output", "Verified exit evidence"],
            "key": "intake-" + suffix})["intake"]
        record["intake_id"] = intake["id"]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(repo / "host")
        # repo here is hub2; host package must take priority over the old Hub package.
        log = (output / "host.log").open("w")
        private_file(output / "host.log")
        agent = subprocess.Popen([sys.executable, "-m", "hub2_agent_hub_host.cli", "--config", str(machine_path), "serve"],
                                 env=environment, stdout=log, stderr=log, start_new_session=os.name != "nt")
        started = operator.request("POST", "/api/intakes/" + intake["id"] + "/start?" + urlencode({"project": project}), {
            "machine_id": machine, "key": "start-" + suffix,
            "workspace": str(workspace), "engine": "opencode", "model": "opencode-go/muse-spark-1.3-contributor",
            "prompt": prompt, "interactive": False, "timeout_seconds": 120})
        record["session_id"] = started["session"]["id"]
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            require(agent.poll() is None, "host_agent_exited_before_completion")
            current = operator.request("GET", "/api/intakes/" + intake["id"] + "?" + urlencode({"project": project}))["intake"]
            if current["status"] in {"completed-awaiting-review", "failed"}:
                break
            time.sleep(2)
        else:
            raise RuntimeError("guarded_roundtrip_timeout")
        evidence = current.get("final_evidence") or {}
        require(current["status"] == "completed-awaiting-review", "native_work_failed")
        require(evidence.get("exit_code") == 0, "exit_zero_unproven")
        require(hello.read_text() == "Hub2 guarded dev E2E passed", "native_artifact_mismatch")
        record["native_id"] = evidence["native_id"]
        account_after=operator.request("GET","/api/account")
        require(account_after.get("account",{}).get("occupied")==0,"verified_finish_account_slot_not_released")
        record["stages"].append("worker_socket_host_maintained_model_finish_intake_roundtrip")
        record["evidence"] = evidence
        metrics = {}
        for kind, identity in (("control", "global"), ("project", project), ("machine", machine), ("account", args.account_id)):
            # Metrics is a test-only authorized composition route, never a forged actor header.
            import urllib.request
            request = urllib.request.Request(server + "/e2e/metrics?" + urlencode({"kind": kind, "id": identity}),
                                             headers={"Authorization": "Bearer " + cfg["token"]})
            with urllib.request.urlopen(request, timeout=15) as response:
                metrics[kind] = json.load(response)
        request=urllib.request.Request(server+'/e2e/status?'+urlencode({'kind':'machine','id':machine}),headers={'Authorization':'Bearer '+cfg['token']})
        with urllib.request.urlopen(request,timeout=15) as response:
            status=json.load(response)
        require(not status.get('pending_notifications'),'durable_notifications_not_forwarded')
        record["metered_instance_counters"] = metrics
        record["cost_limit"] = "Development-instance counters only; no 24h production-budget claim"
        record["passed"] = True
        return record
    except (ApiError, RuntimeError) as error:
        record["failure"] = str(error)
        raise
    finally:
        if agent and agent.poll() is None:
            if os.name == "nt":
                agent.terminate()
            else:
                os.killpg(agent.pid, signal.SIGTERM)
            try:
                agent.wait(timeout=10)
            except subprocess.TimeoutExpired:
                agent.kill()
                agent.wait()
        if not record.get("passed") and record.get("session_id"):
            # This uniquely named native task and disposable workspace belong to this test.
            tag = "task-hub2-" + record["session_id"]
            status = subprocess.run(["a", "status", str(workspace) + ":" + tag, "--json"],
                                    capture_output=True, text=True, timeout=15)
            if status.returncode == 0:
                native = json.loads(status.stdout)
                if native.get("tag") == tag and native.get("workspace") == str(workspace) and native.get("id"):
                    killed = subprocess.run(["a", "kill", native["id"]], capture_output=True, timeout=15)
                    record["owned_native_cleanup_exit"] = killed.returncode
        write_private(output / "result.json", record)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--operator-config", type=Path, required=True)
    parser.add_argument("--launcher-config", type=Path, required=True)
    parser.add_argument("--go-device-export", type=Path, required=True)
    parser.add_argument("--account-id", default="e2e-muse")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run(args)
        print(json.dumps({"passed": result["passed"], "stages": result["stages"], "native_id": result["native_id"]}))
        return 0
    except Exception as error:
        # Private logs preserve service details; terminal output never contains credentials.
        print(json.dumps({"passed": False, "error_type": type(error).__name__, "stage": "inspect_private_result"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
