# Add native Hub engine subscriptions (zcodex/opencode/grok/antigravity) to a Windows host.
# User scope only. Backs up, validates with the host's own load_settings, writes.
param([string[]]$Engines=@('zcodex','opencode','grok','antigravity'))
$ErrorActionPreference='Continue'
$cfg="$env:APPDATA\agent-hub\subscriptions.json"
$proc=Get-CimInstance Win32_Process | ?{ $_.CommandLine -match 'agent_hub_host\.session_host' -and $_.ExecutablePath -match '\\venv\\Scripts\\python' } | select -First 1
if(-not $proc){ $proc=Get-CimInstance Win32_Process | ?{ $_.CommandLine -match 'agent_hub_host' } | select -First 1 }
$py=$proc.ExecutablePath; $env:PYTHONPATH=(Join-Path (Split-Path (Split-Path (Split-Path $py))) "package\host"); "host python: $py (PYTHONPATH=$env:PYTHONPATH)"
function Find($names){ foreach($n in $names){ $c=Get-Command $n -ea 0 | select -First 1; if($c){ return $c.Source }; foreach($d in "$env:USERPROFILE\bin","$env:USERPROFILE\.local\bin","$env:USERPROFILE\.grok\bin","$env:APPDATA\npm","$env:LOCALAPPDATA\Programs\Antigravity\bin","$env:LOCALAPPDATA\Programs\agy"){ foreach($e in '.exe','.cmd',''){ if(Test-Path "$d\$n$e"){ return "$d\$n$e" } } } }; $null }
function Adm($svc,$acct,$eng){ ,@($py,'-c',"import sys,json; from agent_hub_host.native_admission import probe; print(json.dumps(probe(json.loads(sys.stdin.read(32769) or 'null'),'$svc','$acct','$eng')))") }
$defs=@{
 zcodex=@{service='zai';account_label='shared-zai';engine='zcodex';bin=@('zcodex');launcher_provider='zai';launcher_model='glm-5.3-flash';launcher_effort='high';extra=@('--dangerously-bypass-approvals-and-sandbox','-c','check_for_update_on_startup=false')}
 grok=@{service='grok';account_label='shared-grok';engine='grok';bin=@('grok')}
 opencode=@{service='go';account_label='shared-go';engine='opencode';bin=@('opencode')}
 antigravity=@{service='gemini';account_label='shared-gemini';engine='antigravity';bin=@('agy','antigravity')}
}
$j=Get-Content $cfg -Raw | ConvertFrom-Json
$subs=[System.Collections.ArrayList]@(@($j.subscriptions) | ?{ $_ })
foreach($e in $Engines){ $d=$defs[$e]; $b=Find $d.bin
 if(-not $b){ "SKIP ${e}: binary not found"; continue }
 if($subs | ?{ $_.account_label -eq $d.account_label }){ "KEEP ${e}: already present"; continue }
 $s=[ordered]@{service=$d.service;account_label=$d.account_label;engine=$d.engine;admission_argv=(Adm $d.service $d.account_label $d.engine)}
 if($d.launcher_provider){ $s.launcher_provider=$d.launcher_provider;$s.launcher_model=$d.launcher_model;$s.launcher_effort=$d.launcher_effort }
 if($d.extra){ $s.startup_argv=@($b)+$d.extra }
 # validate this entry alone on top of current list
 $trial=$j.PSObject.Copy(); $trial.subscriptions=@($subs)+@([pscustomobject]$s)
 $tmp="$env:TEMP\subs-trial-$e.json"; $trial | ConvertTo-Json -Depth 20 | %{ [IO.File]::WriteAllText($tmp,$_,(New-Object Text.UTF8Encoding $false)) }
 & $py -c "import sys; from agent_hub_host import fleet; fleet.load_settings(sys.argv[1]); print('valid')" $tmp 2>&1 | Out-String | %{ if($_ -cmatch '(?m)^valid'){ [void]$subs.Add([pscustomobject]$s); "ADD ${e}: $b" } else { "DROP ${e}: validation failed: $(($_ -split "`n" | ?{$_ -match "Error"}) -join " ")" } }
 Remove-Item $tmp -ea 0 }
$bak="$cfg.bak-engines-1031"; if(-not (Test-Path $bak)){ Copy-Item $cfg $bak }
$j.subscriptions=@($subs); $out=$j | ConvertTo-Json -Depth 20
[IO.File]::WriteAllText($cfg,$out,(New-Object Text.UTF8Encoding $false))
& $py -c "import sys; from agent_hub_host import fleet; s=fleet.load_settings(sys.argv[1]); print('final engines:', sorted({x.get('engine',x['service']) for x in s['subscriptions']}))" $cfg
