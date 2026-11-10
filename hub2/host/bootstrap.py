"""Install the native CLI and pinned transport in an isolated user environment."""
import os
from pathlib import Path
import subprocess
import shutil
import sys
import venv

PACKAGE_NAME = 'agent-hub-host-v2'


def main():
    base=Path(os.environ.get('LOCALAPPDATA') or Path.home()/'.local/share')/'agent-hub2/venv'
    python=base/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    uv=shutil.which('uv')
    if uv:
        subprocess.run([uv,'venv','--python',sys.executable,'--allow-existing',str(base)],check=True,capture_output=True)
        installer=[uv,'pip','install','--python',str(python),
                   '--reinstall-package',PACKAGE_NAME]
    else:
        if not python.exists():venv.EnvBuilder(with_pip=True).create(base)
        installer=[str(python),'-m','pip','install','--force-reinstall',
                   '--disable-pip-version-check']
    result=subprocess.run([*installer,str(Path(__file__).parent.resolve())],capture_output=True,text=True)
    if result.returncode:
        print('Host installation failed; no working release recorded.',file=sys.stderr)
        return 1
    command=base/('Scripts/hub2.exe' if os.name=='nt' else 'bin/hub2')
    subprocess.run([str(command),'--help'],check=True,stdout=subprocess.DEVNULL)
    print(command)
    return 0


if __name__=='__main__':raise SystemExit(main())
