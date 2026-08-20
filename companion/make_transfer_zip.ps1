$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$Source = 'H:\RadioTEDU-Services-Portable'
$Desktop = [Environment]::GetFolderPath('Desktop')
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Out = Join-Path $Desktop "RadioTEDU-Services-Transfer-$Stamp.zip"
$Secret = Join-Path $Desktop 'RadioTEDU-Services-Secrets.txt'
$Prompt = Join-Path $PSScriptRoot 'CODEX-HANDOFF-PROMPT.md'

$File = [IO.File]::Open($Out, [IO.FileMode]::CreateNew)
try {
  $Zip = [IO.Compression.ZipArchive]::new($File, [IO.Compression.ZipArchiveMode]::Create, $false)
  try {
    foreach ($Item in Get-ChildItem -LiteralPath $Source -File -Recurse) {
      $Relative = $Item.FullName.Substring($Source.Length).TrimStart('\').Replace('\','/')
      [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($Zip, $Item.FullName, $Relative, [IO.Compression.CompressionLevel]::Optimal) | Out-Null
    }
    [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($Zip, $Secret, 'RadioTEDU-Services-Secrets.txt', [IO.Compression.CompressionLevel]::Optimal) | Out-Null
    [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($Zip, $Prompt, 'CODEX-HANDOFF-PROMPT.md', [IO.Compression.CompressionLevel]::Optimal) | Out-Null
  } finally {
    $Zip.Dispose()
  }
} finally {
  $File.Dispose()
}

$Acl = New-Object Security.AccessControl.FileSecurity
$Acl.SetAccessRuleProtection($true, $false)
$User = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$Acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($User, 'FullControl', 'Allow')))
$Acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule('NT AUTHORITY\SYSTEM', 'FullControl', 'Allow')))
Set-Acl -LiteralPath $Out -AclObject $Acl
Write-Host $Out
