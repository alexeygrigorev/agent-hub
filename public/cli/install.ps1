# Developer Hub CLI one-line installer for Windows (user-local only, no admin, no secrets).
#
#   irm <base>/cli/install.ps1 | iex
#
# Env overrides:
#   HUB_BASE_URL     hub base, default https://hub.dtcdev.click
#   HUB_INSTALL_DIR  install dir, default $env:LOCALAPPDATA\Programs\hub
#
# Installs $HUB_INSTALL_DIR/hub.pyz, $HUB_INSTALL_DIR/hub.cmd, and $HUB_INSTALL_DIR/hub.ps1.

$ErrorActionPreference = 'Stop'

$BaseDefault = 'https://hub.dtcdev.click'
$BaseUrl = if ($env:HUB_BASE_URL) { $env:HUB_BASE_URL.TrimEnd('/') } else { $BaseDefault }

$DefaultInstallDir = if ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA 'Programs\hub'
} else {
    Join-Path $HOME '.local\bin'
}
$InstallDir = if ($env:HUB_INSTALL_DIR) { $env:HUB_INSTALL_DIR } else { $DefaultInstallDir }

function Fail($msg) {
    Write-Error "hub install failed: $msg"
    exit 1
}

# Require user-local install directory
if ($InstallDir -match '^[A-Za-z]:\\(Windows|Program Files|Program Files \(x86\))\\?') {
    Fail "refusing privileged system install dir: $InstallDir"
}

# Locate python command and verify version >= 3.9
$PythonExe = $null
$PythonArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonExe = 'py'
    $PythonArgs = @('-3')
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonExe = 'python'
} elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
    $PythonExe = 'python3'
} else {
    Fail "Python 3 is required. Please install Python 3.9 or higher."
}

$PyVerCheck = & $PythonExe @PythonArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>$null
if ($LASTEXITCODE -ne 0) {
    Fail "Python >= 3.9 is required."
}

$WorkDir = Join-Path ([System.IO.Path]::GetTempPath()) ("hub-install-" + [System.Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $WorkDir -Force | Out-Null

try {
    # Download CLI distribution assets
    $Assets = @('hub.pyz', 'hub.pyz.sha256', 'manifest.json')
    foreach ($f in $Assets) {
        $Url = "$BaseUrl/cli/$f"
        $OutFile = Join-Path $WorkDir $f
        Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
        if (-not (Test-Path $OutFile)) {
            Fail "failed to download $Url"
        }
    }

    $PyzFile = Join-Path $WorkDir 'hub.pyz'
    $ShaFile = Join-Path $WorkDir 'hub.pyz.sha256'
    if ((Get-Item $PyzFile).Length -eq 0) {
        Fail "downloaded hub.pyz is empty"
    }

    # Verify SHA256 checksum
    $ShaContent = (Get-Content $ShaFile -Raw).Trim()
    $ExpectedSha = ($ShaContent -split '\s+')[0].ToLowerInvariant()
    $ActualHash = (Get-FileHash -Path $PyzFile -Algorithm SHA256).Hash.ToLowerInvariant()

    if ($ActualHash -ne $ExpectedSha) {
        Fail "sha256 mismatch (manifest $ExpectedSha vs actual $ActualHash)"
    }

    # Ensure destination directory exists
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null

    $DestPyz = Join-Path $InstallDir 'hub.pyz'
    Copy-Item -Path $PyzFile -Destination $DestPyz -Force

    # Create hub.cmd batch wrapper
    $CmdWrapper = Join-Path $InstallDir 'hub.cmd'
    $CmdContent = @"
@echo off
setlocal
where py >nul 2>nul
if %errorlevel% equ 0 goto use_py
python "%~dp0hub.pyz" %*
exit /b %errorlevel%

:use_py
py -3 "%~dp0hub.pyz" %*
exit /b %errorlevel%
"@
    Set-Content -Path $CmdWrapper -Value $CmdContent -Encoding ASCII

    # Create hub.ps1 PowerShell wrapper
    $Ps1Wrapper = Join-Path $InstallDir 'hub.ps1'
    $Ps1Content = @"
`$Py = if (Get-Command py -ErrorAction SilentlyContinue) { 'py' } else { 'python' }
if (`$Py -eq 'py') {
    & py -3 "`$PSScriptRoot\hub.pyz" @args
} else {
    & python "`$PSScriptRoot\hub.pyz" @args
}
exit `$LASTEXITCODE
"@
    Set-Content -Path $Ps1Wrapper -Value $Ps1Content -Encoding UTF8

    Write-Host "Installed hub to $InstallDir"
    if ($env:PATH -notlike "*$InstallDir*") {
        Write-Host "Add to PATH: `$env:PATH = `"$InstallDir;`$env:PATH`""
    }
    Write-Host "Next: hub auth login [--url $BaseUrl]"
} finally {
    if (Test-Path $WorkDir) {
        Remove-Item -Path $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
