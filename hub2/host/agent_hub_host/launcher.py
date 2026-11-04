"""Adapter to the maintained launcher CLI; admission and spawn stay atomic there."""
import json
import hashlib
import os
import subprocess
import shutil
import sys
from pathlib import Path
from .client import private_file


def same_workspace(left, right):
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        left_path, right_path = Path(left), Path(right)
        return left_path.is_absolute() and right_path.is_absolute() and left_path.samefile(right_path)
    except (OSError, RuntimeError, ValueError):
        return False


def native_returncode(value):
    if not isinstance(value,dict):return None
    code,signal=value.get('code'),value.get('signal')
    if type(code) is int and -2147483648<=code<=2147483647 and signal is None:
        return code
    if 'code' in value and code is None and type(signal) is int and 1<=signal<=127:
        return -signal
    return None


class Launcher:
    def __init__(self, configuration, runner=subprocess.run):
        self.config, self.runner = configuration, runner

    def invoke(self, *arguments, timeout=30, workspace=None, controller=None):
        config = self.config
        argv = config.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
            raise RuntimeError("maintained_launcher_not_configured")
        env = os.environ.copy()
        for key in ('HUB2_TOKEN','HUB2_SERVER','HUB2_CONFIG'):env.pop(key,None)
        env.pop('HUB2_LAUNCH_CONTEXT',None)
        if controller:env['HUB2_LAUNCH_CONTEXT']=controller
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [config.get("repository"), env.get("PYTHONPATH")]))
        if workspace:
            env["PWD"] = str(Path(workspace).resolve())
        result = self.runner(argv + ["--config-dir", config["config_dir"], *arguments],
                             text=True, capture_output=True, timeout=timeout, check=False, env=env, cwd=workspace)
        if result.returncode or len(result.stdout) > 262144:
            raise RuntimeError("maintained_launcher_refused")
        try:
            document = json.loads(result.stdout)
            if isinstance(document, dict):
                return [document]
        except ValueError:
            pass
        documents = []
        for line in result.stdout.splitlines():
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    documents.append(item)
            except ValueError:
                continue
        return documents

    def launch(self, task_id, specification):
        route = self.config.get("routes", {}).get(specification["engine"])
        if not isinstance(route, dict):
            raise RuntimeError("maintained_launcher_route_missing")
        required={name for name,needed in [('interactive',specification.get('interactive')),('goal',specification.get('with_goal')),('effort',specification.get('effort') is not None)] if needed}
        required.add('interactive' if specification.get('interactive') else 'headless')
        if not required.issubset(self.capabilities().get(specification['engine'],set())):
            raise RuntimeError('maintained_launcher_capability_unavailable')
        # All credential/profile/admission argv comes from private host configuration,
        # never task text or a remotely supplied executable command.
        outcome = Path(specification['workspace']) / '.local' / 'hub2-results' / (task_id+'.json')
        outcome.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        if not outcome.parent.resolve().is_relative_to(Path(specification['workspace']).resolve()) or outcome.exists():
            raise RuntimeError('native_outcome_path_conflict')
        private_file(outcome.parent)
        goal = (f"Your assigned workspace is {specification['workspace']}. Use this exact working directory "
                "for every tool call, including the launcher first-action aplexer whoami proof. "
                "Do not run that proof from your home directory or an enclosing checkout. "
                f"After the assigned work, write an honest JSON outcome summary and evidence paths to {outcome}; "
                "this is private controller evidence, not acceptance. Never include credentials.\n\n" + specification["prompt"])
        if specification.get('child_config_file'):
            cli=self.config.get('hub_cli') or [str(Path(sys.executable).with_name('hub2.exe' if os.name=='nt' else 'hub2'))]
            goal+='\nAssigned least-privilege Hub CLI argv: '+json.dumps([*cli,'--config',specification['child_config_file']])+'. After the mandatory launcher identity proof, read your assigned intake and acceptance criteria with this CLI before other task work. Use this private config for assigned intake and mailbox operations. Never print or copy its token.'
            goal+=' Assigned context: '+json.dumps(specification.get('worker_context',{}))+'.'
        payload = {**route, "goal": goal, "owner": "hub2-host",
                   "cwd": specification["workspace"], "timeout": specification["timeout_seconds"],
                   "mode": "interactive" if specification["interactive"] else "headless"}
        if specification.get('provider_home'):
            payload['provider_home']=specification['provider_home']
            payload['account_label']=specification['account_id']
        if specification.get('with_goal'):payload['with_goal']=True
        if specification.get('effort') is not None:payload['effort']=specification['effort']
        if specification.get('child_config_file'):
            payload['child_config_file']=specification['child_config_file']
            payload['runtime_deadline']=specification['runtime_deadline']
        if specification["model"]:
            payload["model_requirements"] = {**payload.get("model_requirements", {}), "models": [specification["model"]]}
        self.invoke("submit", "--id", task_id, "--key", task_id,
                    "--payload", json.dumps(payload), "--paths",str(outcome),"--json", workspace=specification["workspace"])
        tmpdir = Path(specification["workspace"]) / ".local" / "tmp" / task_id
        tmpdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        arguments = ["run", "--id", task_id, "--cwd", specification["workspace"],
                     "--tmpdir", str(tmpdir), "--backend", "aplexer"]
        if self.config.get("resource_config"):
            arguments += ["--resource-config", self.config["resource_config"]]
        documents = self.invoke(*arguments, timeout=min(specification["timeout_seconds"] + 30, 7230), workspace=specification["workspace"],controller=specification.get('controller_config'))
        receipts = [document.get("receipt", {}) for document in documents]
        valid = [receipt for receipt in receipts if receipt.get("first_action_verified") is True
                 and isinstance(receipt.get("session_id"), str)]
        if len(valid) != 1:
            raise RuntimeError("maintained_launcher_receipt_unverified")
        if specification["model"] and (valid[0].get("model_verified") is not True or valid[0].get("model") != specification["model"]):
            raise RuntimeError("maintained_launcher_model_unverified")
        self.verify_modes(valid[0],specification)
        return valid[0]

    def capabilities(self):
        result={engine:set() for engine in self.config.get('routes',{})}
        manifest=self.config.get('capabilities_manifest')
        if not manifest:return result
        try:
            with Path(manifest).open('rb') as stream:raw=stream.read(65537)
            if len(raw)>65536:return result
            data=json.loads(raw)
            if data.get('version')!=3 or set(data['files'])!={'launcher/launch.py','launcher/cli.py','launcher/interactive.py','launcher/provider_home.py'}:return result
            if not isinstance(data.get('providers'),dict) or any(not isinstance(v,list) or not set(v).issubset({'headless','interactive','goal','effort'}) for v in data['providers'].values()):return result
            native=Path(shutil.which('aplexer') or '').resolve(strict=True)
            if native.name not in {'aplexer','a','aplexer.exe','a.exe'}:return result
            if native.stat().st_size>64*1024*1024:return result
            executable=native.read_bytes()
            if executable[:2]!=b'MZ' and executable[:4] not in (b'\x7fELF',b'\xcf\xfa\xed\xfe',b'\xfe\xed\xfa\xcf',b'\xca\xfe\xba\xbe'):return result
            if hashlib.sha256(executable).hexdigest()!=data.get('native_sha256'):return result
            repository=Path(self.config['repository'])
            if any((repository/name).stat().st_size>2*1024*1024 or hashlib.sha256((repository/name).read_bytes()).hexdigest()!=digest for name,digest in data['files'].items()):return result
            for engine,route in self.config.get('routes',{}).items():
                providers=route.get('model_requirements',{}).get('providers',[]) or ([route['provider']] if isinstance(route.get('provider'),str) else [])
                if providers:
                    for capability in ('headless','interactive','goal','effort'):
                        if all(capability in data.get('providers',{}).get(provider,[]) for provider in providers):result[engine].add(capability)
        except (OSError,KeyError,TypeError,ValueError):pass
        return result

    @staticmethod
    def verify_modes(receipt,specification):
        if specification.get('interactive') and (receipt.get('interactive_verified') is not True or receipt.get('mode')!='interactive'):
            raise RuntimeError('maintained_interactive_unverified')
        if specification.get('effort') is not None and (receipt.get('effort_verified') is not True or receipt.get('effort')!=specification['effort']):
            raise RuntimeError('maintained_effort_unverified')
        if specification.get('with_goal'):
            proof=receipt.get('goal_registration')
            if not isinstance(proof,dict) or not isinstance(proof.get('thread_id'),str) or not 1<=len(proof['thread_id'])<=128 or not isinstance(proof.get('objective_hash'),str) or len(proof['objective_hash'])!=64 or any(c not in '0123456789abcdef' for c in proof['objective_hash']) or proof.get('status') not in {'active','complete','paused','blocked','stalled','usage_limited','budget_limited'}:
                raise RuntimeError('maintained_goal_unverified')

    def retired(self, native_id, expected):
        if not self.config.get("repository"):
            return None
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [self.config["repository"], env.get("PYTHONPATH")]))
        script = """
import json,sys
from launcher.native_lifecycle import read_tombstone,verify_tombstone
from agent_hub_host.launcher import native_returncode
v=json.load(sys.stdin);t=read_tombstone(v['id'])
ok,_=verify_tombstone(t,v['id'],v['tag'],v['workspace'])
valid=ok and t.get('containment_empty') is True and native_returncode(t.get('exit')) is not None
print(json.dumps({'id':t['id'],'tag':t['tag'],'workspace':t['workspace'],'phase':'exited','containment_empty':True,'exit':t['exit'],'deadline_ms':t.get('deadline_ms'),'deadline_exceeded':t.get('deadline_exceeded'),'retirement':'verified_finished'} if valid else None))
"""
        result = self.runner([sys.executable, "-c", script], input=json.dumps(
            {"id": native_id, "tag": expected["tag"], "workspace": expected["workspace"]}),
            text=True, capture_output=True, timeout=15, check=False, env=env)
        return json.loads(result.stdout) if not result.returncode and len(result.stdout) < 4096 else None

    def recover(self, task_id, workspace, requirements=None):
        documents = self.invoke("status", "--id", task_id, workspace=workspace)
        for document in documents:
            binding = document.get("native_binding", {})
            details = binding.get("details", {})
            if (same_workspace(binding.get("workspace"), workspace) and binding.get("tag") == "task-" + task_id
                    and details.get("first_action_validated_at") and isinstance(binding.get("native_id"), str)):
                if requirements and any(requirements.get(key) for key in ('interactive','with_goal','effort')):
                    with (Path(workspace)/'.local'/('launch-'+task_id+'.json')).open('rb') as stream:raw=stream.read(262145)
                    if len(raw)>262144:raise RuntimeError('maintained_recovery_evidence_too_large')
                    record=json.loads(raw)
                    if record.get('session_id')!=binding['native_id'] or not record.get('first_action_validated_at'):raise RuntimeError('maintained_recovery_identity_unverified')
                    self.verify_modes(record,requirements)
                return binding["native_id"]
        raise RuntimeError("maintained_launch_recovery_unverified")

    def aborted_identity(self, task_id, workspace):
        """Birth binding identifies what may be stopped, never successful start."""
        for document in self.invoke('status','--id',task_id,workspace=workspace):
            binding=document.get('native_binding',{})
            if same_workspace(binding.get('workspace'),workspace) and binding.get('tag')=='task-'+task_id and isinstance(binding.get('native_id'),str):
                return binding['native_id']
        return None

    def finalize(self, task_id, workspace):
        documents=self.invoke('reconcile','--id',task_id,'--json',workspace=workspace)
        for document in documents:
            if document.get('native_status') in {'dead','retired-finished'} and document.get('resources_reserved') is False:
                return document.get('state')
        raise RuntimeError('maintained_native_retirement_unverified')
