# irm https://hub.dtcdev.click/host/install.ps1 | iex
# Per-user only: no RunAs, machine PATH, MSI or elevated scheduled task.
$ErrorActionPreference = 'Stop'
$Base = if ($env:HUB_BASE_URL) { $env:HUB_BASE_URL.TrimEnd('/') } else { 'https://hub.dtcdev.click' }
$Root = Join-Path $env:LOCALAPPDATA 'Programs\agent-hub'
$Bin = Join-Path $Root 'bin'
$TempRoot = Join-Path $Root 'tmp'
New-Item -ItemType Directory -Force -Path $Bin,$TempRoot | Out-Null
$Work = Join-Path $TempRoot ([guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Work | Out-Null
try {
    $Uv = Join-Path $Bin 'uv.exe'
    if (-not (Test-Path -LiteralPath $Uv)) {
        $OldUvInstall = $env:UV_UNMANAGED_INSTALL
        try {
            $env:UV_UNMANAGED_INSTALL = $Bin
            $UvInstaller = Join-Path $Work 'uv-install.ps1'
            Invoke-WebRequest 'https://astral.sh/uv/0.13.6/install.ps1' -OutFile $UvInstaller -UseBasicParsing
            if ((Get-FileHash $UvInstaller -Algorithm SHA256).Hash.ToLowerInvariant() -ne 'd00580051699bee58a2e2d9b925f45788b35f7e0fff7966fd5bedfc3c547dc7d') {
                throw 'uv installer checksum mismatch'
            }
            # uv's installer calls exit, which would terminate a piped
            # `irm | iex` session; run it in a child process so the bootstrap keeps running.
            $Child = (Get-Process -Id $PID).Path
            & $Child -NoProfile -ExecutionPolicy Bypass -File $UvInstaller
            if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Uv)) { throw 'uv installation failed' }
        } finally { $env:UV_UNMANAGED_INSTALL = $OldUvInstall }
    }
    $env:PATH = "$Bin;$env:PATH"
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $Root 'python'
    $env:UV_CACHE_DIR = Join-Path $Root 'cache\uv'
    $env:TMP = $TempRoot
    $env:TEMP = $TempRoot
    $Archive = Join-Path $Work 'hub.pyz'
    Invoke-WebRequest "$Base/cli/hub.pyz" -OutFile $Archive -UseBasicParsing
    Invoke-WebRequest "$Base/cli/hub.pyz.sha256" -OutFile "$Archive.sha256" -UseBasicParsing
    $Expected = ((Get-Content "$Archive.sha256" -Raw).Trim() -split '\s+')[0]
    if ($Expected -notmatch '^[a-fA-F0-9]{64}$') { throw 'Invalid Hub checksum file' }
    if ((Get-FileHash $Archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Expected.ToLowerInvariant()) {
        throw 'Hub download checksum mismatch'
    }
    # x64 Python runs under WoA emulation when native client wheels are absent;
    # native Node/agent binaries still use the actual ARM64 host architecture.
    $Python = 'cpython-3.12-windows-x86_64'
    # A file avoids Windows PowerShell 5.1 removing embedded quotes while
    # passing a Python -c expression through its native argument marshaler.
    $Entry = Join-Path $Work 'bootstrap.py'
    @'
import sys
sys.path.insert(0, sys.argv[1])
from agent_hub_host.bootstrap import main
main(['--archive', sys.argv[1], '--base-url', sys.argv[2], '--uv', sys.argv[3]])
'@ | Set-Content -LiteralPath $Entry -Encoding UTF8
    & $Uv run --managed-python --no-project --python $Python python $Entry $Archive $Base $Uv
    if ($LASTEXITCODE -ne 0) { throw 'Hub host setup did not complete; rerun the same command to resume.' }
    $UserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:PATH = "$UserPath;$env:PATH"
    $env:CLAUDE_CODE_GIT_BASH_PATH = [Environment]::GetEnvironmentVariable('CLAUDE_CODE_GIT_BASH_PATH', 'User')
} finally {
    Remove-Item -LiteralPath $Work -Recurse -Force -ErrorAction SilentlyContinue
}
