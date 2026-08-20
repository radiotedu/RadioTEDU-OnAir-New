$ErrorActionPreference = 'Stop'
$Desktop = [Environment]::GetFolderPath('Desktop')
$Out = Join-Path $Desktop 'RadioTEDU-Services-Secrets.txt'
$Connections = Join-Path $PSScriptRoot 'connections.json'
$Sources = @(
  @{ Name = 'PORTABLE CONNECTION MAP'; Path = $Connections },
  @{ Name = 'VOTING SERVICE ENVIRONMENT'; Path = 'C:\ProgramData\RadioTEDU\OnAir\secrets\voting.env' },
  @{ Name = 'JUKELOCAL SERVICE ENVIRONMENT'; Path = 'C:\ProgramData\RadioTEDU\OnAir\secrets\juke-local.env' },
  @{ Name = 'AI STATIONS CONNECTION CONFIG'; Path = 'C:\ProgramData\RadioTEDU\OnAir\services\RadioTEDU.AIStreams.json' }
)

$Writer = [IO.StreamWriter]::new($Out, $false, [Text.UTF8Encoding]::new($true))
try {
  $Writer.WriteLine('RADIOTEDU SERVICES CONFIDENTIAL HANDOFF')
  $Writer.WriteLine('Created: ' + [DateTimeOffset]::Now.ToString('o'))
  $Writer.WriteLine('Streaming PC LAN IP: 10.10.1.200')
  $Writer.WriteLine('OnAir API: http://10.10.1.200:18110')
  $Writer.WriteLine('Origin: 10.98.98.75:11154')
  $Writer.WriteLine('Public stream base: http://stream.radiotedu.com:11154')
  $Writer.WriteLine('WARNING: PLAINTEXT SECRETS. Transfer securely and delete this file afterward.')
  foreach ($Source in $Sources) {
    $Writer.WriteLine("`r`n===== $($Source.Name) =====")
    if (Test-Path -LiteralPath $Source.Path) {
      $Writer.Write((Get-Content -LiteralPath $Source.Path -Raw))
    } else {
      $Writer.WriteLine('[not present on this PC] ' + $Source.Path)
    }
  }
} finally {
  $Writer.Dispose()
}

$Acl = New-Object Security.AccessControl.FileSecurity
$Acl.SetAccessRuleProtection($true, $false)
$User = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$Acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($User, 'FullControl', 'Allow')))
$Acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule('NT AUTHORITY\SYSTEM', 'FullControl', 'Allow')))
Set-Acl -LiteralPath $Out -AclObject $Acl
Write-Host $Out
