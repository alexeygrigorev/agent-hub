"""Actual Worker/WS/scoped CLI seam for the public native fixture; no model launch."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
from hub2_agent_hub_host.agent import Bridge,Journal
from hub2_agent_hub_host.client import Client,save_config


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--host-config',type=Path,required=True)
    parser.add_argument('--offer',type=Path,required=True)
    parser.add_argument('--launcher-root',type=Path,required=True)
    args=parser.parse_args()
    config=json.loads(args.host_config.read_text());offer=json.loads(args.offer.read_text())
    root=args.host_config.parent.resolve();workspace=Path(offer['launch']['workspace']).resolve()
    if not workspace.is_relative_to(root):raise ValueError('fixture_workspace_outside_private_root')
    workspace.mkdir(parents=True,exist_ok=True)
    client=Client(config['server'],config['token'])
    repository=args.launcher_root.resolve(strict=True)
    names=('launcher/launch.py','launcher/cli.py','launcher/interactive.py','launcher/provider_home.py')
    manifest=root/'actual-launcher-headless-capabilities.json'
    engine=offer['launch']['engine']
    provider={'codex':'codex','opencode':'opencode','claude':'claude'}[engine]
    save_config(manifest,{'version':3,'native_sha256':hashlib.sha256(Path(shutil.which('aplexer')).read_bytes()).hexdigest(),'files':{name:hashlib.sha256((repository/name).read_bytes()).hexdigest() for name in names},
                          'providers':{provider:['headless']},'fixture_scope':'auth/context/transport only; no native model assertion'})
    journal=Journal(root/'host-journal.sqlite')
    bridge=Bridge(client,journal,[root],[offer['launch']['engine']],executable=sys.executable,
                  launcher_config={'repository':str(repository),'capabilities_manifest':str(manifest),
                                   'routes':{engine:{'provider':provider}}},machine_id=config['machine_id'])
    try:
        launch=bridge.validate_launch(offer)
        context=bridge.child_context(offer,launch)
        private=json.loads(Path(context).read_text())
        assert private['token']!=config['token']
        if os.name!='nt':assert Path(context).stat().st_mode&0o777==0o600
        with client.socket(config['machine_id']) as channel:
            for _ in range(5):
                frame=json.loads(channel.recv(timeout=5))
                if frame.get('type')=='session.offer' and frame.get('offer',{}).get('session_id')==offer['session_id']:
                    assert frame['offer']['epoch']==offer['epoch'];break
            else:raise RuntimeError('actual_machine_socket_offer_unproven')
        env=os.environ.copy()
        for key in ('HUB2_TOKEN','HUB2_SERVER'):env.pop(key,None)
        own='/api/intakes/'+offer['intake_id']+'?project='+offer['project_id']
        result=subprocess.run([sys.executable,'-m','hub2_agent_hub_host.cli','--config',context,'request','GET',own],
                              env=env,capture_output=True,text=True,timeout=15)
        assert result.returncode==0,'assigned_child_read_denied'
        for secret in (config['token'],private['token']):assert secret not in result.stdout+result.stderr
        return {'passed':True,'actual_host_scoped_file':True,'actual_worker_websocket':True,'actual_child_cli_read':True,
                'literal_child_token_in_native_metadata':False,'model_launched':False}
    finally:journal.db.close()


if __name__=='__main__':
    print(json.dumps(main()))
