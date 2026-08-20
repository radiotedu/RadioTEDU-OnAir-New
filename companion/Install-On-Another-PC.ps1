$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Payload = Join-Path $Here 'payload'
$Runtime = Join-Path $env:ProgramData 'RadioTEDU\ServicesCompanion'
$Secrets = Join-Path $Runtime 'secrets'
$Configs = Join-Path $Runtime 'services'
$HostExe = Join-Path $Payload 'service-host\RadioTEDU-OnAir-ServiceHost.exe'

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Start-Process powershell.exe -Verb RunAs -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"' + $PSCommandPath + '"'))
  exit
}

New-Item -ItemType Directory -Force -Path $Runtime,$Secrets,$Configs | Out-Null
if (-not (Get-Command node.exe -ErrorAction SilentlyContinue)) { winget install --id OpenJS.NodeJS.LTS --silent --accept-package-agreements --accept-source-agreements }
if (-not (Get-Command python.exe -ErrorAction SilentlyContinue)) { winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements }
if (-not (Get-Command ollama.exe -ErrorAction SilentlyContinue)) { winget install --id Ollama.Ollama --silent --accept-package-agreements --accept-source-agreements }

$Node = (Get-Command node.exe -ErrorAction Stop).Source
$Python = (Get-Command python.exe -ErrorAction Stop).Source
$Ollama = (Get-Command ollama.exe -ErrorAction Stop).Source
$Voting = Join-Path $Payload 'voting-agent'
$Juke = Join-Path $Payload 'juke-local'
$AI = Join-Path $Payload 'ai-host'

Push-Location $Voting; npm ci --omit=optional; Pop-Location
Push-Location $Juke; npm ci --omit=optional; Pop-Location
& $Python -m pip install --disable-pip-version-check -r (Join-Path $AI 'requirements.txt')

foreach ($pair in @(@('voting-agent\.env.example','voting.env'),@('juke-local\.env.example','juke-local.env'))) {
  $target = Join-Path $Secrets $pair[1]
  if (-not (Test-Path $target)) {
    $template = Join-Path $Payload $pair[0]
    if (Test-Path $template) { Copy-Item $template $target } else { [IO.File]::WriteAllText($target, "# Paste the matching section from RadioTEDU-Services-Secrets.txt here.`r`n") }
  }
}
Copy-Item (Join-Path $Payload 'RadioTEDU.AIStreams.json') (Join-Path $Configs 'RadioTEDU.AIStreams.json') -Force

$Definitions = @{
  'RadioTEDU.JukeLocalMediaAgent' = "# name|executable|arguments|working directory|restart`r`nJuke-Local-Media-Agent|$Node|--env-file=`"$Secrets\juke-local.env`" `"$Juke\server.js`"|$Juke|true`r`n"
  'RadioTEDUVotingRadio' = "# name|executable|arguments|working directory|restart`r`nVoting-Radio-Agent|$Node|`"$Voting\node_modules\tsx\dist\cli.mjs`" --env-file=`"$Secrets\voting.env`" `"$Voting\src\server\index.ts`"|$Voting|true`r`n"
  'RadioTEDU.SharedAI' = "# name|executable|arguments|working directory|restart`r`nOllama-Shared-AI|$Ollama|serve|$(Split-Path $Ollama)|true`r`n"
  'RadioTEDU.AIStreams' = "# name|executable|arguments|working directory|restart`r`nRadioTEDU-AI-Streams|$Python|-u `"$AI\scripts\run_ai_quality_supervisor.py`" --config `"$Configs\RadioTEDU.AIStreams.json`"|$AI|true`r`n"
}
foreach ($name in $Definitions.Keys) {
  $config = Join-Path $Configs "$name.services"
  [IO.File]::WriteAllText($config,$Definitions[$name],[Text.UTF8Encoding]::new($true))
  sc.exe query $name *> $null
  if ($LASTEXITCODE -ne 0) { sc.exe create $name binPath= "`"$HostExe`" --service-name `"$name`" --config `"$config`"" start= delayed-auto | Out-Null }
  sc.exe failure $name reset= 86400 actions= restart/5000/restart/15000/restart/30000 | Out-Null
  sc.exe start $name *> $null
}

$Desktop = [Environment]::GetFolderPath('Desktop')
$Shortcut = Join-Path $Desktop 'RadioTEDU Services.lnk'
$Shell = New-Object -ComObject WScript.Shell
$Link = $Shell.CreateShortcut($Shortcut)
$Link.TargetPath = Join-Path $Here 'RadioTEDU-Services.exe'
$Link.WorkingDirectory = $Here
$Link.Save()
Write-Host 'RadioTEDU Services installed and started. Review local .env files under ProgramData if a component reports not ready.'
