[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OnAirRoot,

    [string]$ServiceName = "RadioTEDU.OnAir.Supervisor",

    [switch]$RequireServiceIdentity,

    [switch]$VerifyOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$systemSid = [System.Security.Principal.SecurityIdentifier]::new("S-1-5-18")
$administratorsSid = [System.Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
$currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$currentSid = $currentIdentity.User.Value
$currentPrincipal = [System.Security.Principal.WindowsPrincipal]::new($currentIdentity)
$isElevated = $currentPrincipal.IsInRole(
    [System.Security.Principal.WindowsBuiltInRole]::Administrator
)
$ownerSid = $administratorsSid
$allowedSids = @(
    $systemSid.Value,
    $administratorsSid.Value
)
if (-not $isElevated) {
    # Repository contract tests and repair diagnostics may run as the
    # interactive operator.  Keep the tree protected and remove inherited
    # Builtin Users access, while retaining the current owner long enough to
    # apply the ACL.  The production installer runs elevated and therefore
    # always uses Administrators as owner.
    $ownerSid = [System.Security.Principal.SecurityIdentifier]::new($currentSid)
    $allowedSids += $currentSid
    if ($RequireServiceIdentity) {
        throw "ACL hardening for a service identity requires an elevated Administrator PowerShell."
    }
}
if ($RequireServiceIdentity) {
    try {
        $serviceAccount = [System.Security.Principal.NTAccount]::new("NT SERVICE", $ServiceName)
        $serviceSid = $serviceAccount.Translate([System.Security.Principal.SecurityIdentifier])
        $allowedSids += $serviceSid.Value
    }
    catch {
        throw "The required service identity 'NT SERVICE\$ServiceName' does not exist."
    }
}
$allowedSids = @($allowedSids | Select-Object -Unique)
$inheritanceFlags = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
    [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
$propagationFlags = [System.Security.AccessControl.PropagationFlags]::None
$fullControl = [System.Security.AccessControl.FileSystemRights]::FullControl
$allow = [System.Security.AccessControl.AccessControlType]::Allow

function New-StrictDirectoryAcl {
    $acl = [System.Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($ownerSid)
    foreach ($sidValue in $allowedSids) {
        $sid = [System.Security.Principal.SecurityIdentifier]::new($sidValue)
        $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
            $sid,
            $fullControl,
            $inheritanceFlags,
            $propagationFlags,
            $allow))
    }
    return $acl
}

function New-StrictFileAcl {
    $acl = [System.Security.AccessControl.FileSecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($ownerSid)
    foreach ($sidValue in $allowedSids) {
        $sid = [System.Security.Principal.SecurityIdentifier]::new($sidValue)
        $acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new(
            $sid,
            $fullControl,
            [System.Security.AccessControl.InheritanceFlags]::None,
            $propagationFlags,
            $allow))
    }
    return $acl
}

function Assert-StrictAcl {
    param([Parameter(Mandatory = $true)][string]$Target)

    # Use the .NET ACL API directly.  Minimal Windows Server/PowerShell images
    # can expose Get-Acl/Set-Acl command metadata while failing to autoload the
    # Microsoft.PowerShell.Security module, which would make installation fail
    # before the service starts.
    $item = Get-Item -LiteralPath $Target -Force
    $verified = $item.GetAccessControl()
    if (-not $verified.AreAccessRulesProtected) {
        throw "ACL inheritance is still enabled for '$Target'."
    }

    $unexpected = @($verified.Access | Where-Object {
        $sid = $_.IdentityReference.Translate(
            [System.Security.Principal.SecurityIdentifier]
        ).Value
        $sid -notin $allowedSids -or
            $_.AccessControlType -ne $allow -or
            ($_.FileSystemRights -band $fullControl) -ne $fullControl
    })
    if ($unexpected.Count -ne 0) {
        throw "ACL contains an unexpected or underprivileged entry for '$Target'."
    }
}

$protectedRelativePaths = @(
    "Backups",
    "CrashDumps",
    "Logs",
    "Recovery",
    "schema-backups",
    "secrets",
    "Services",
    "State"
)

if ($VerifyOnly) {
    if (-not (Test-Path -LiteralPath $OnAirRoot -PathType Container)) {
        throw "The protected RadioTEDU data root does not exist."
    }
    foreach ($relativePath in $protectedRelativePaths) {
        $target = Join-Path $OnAirRoot $relativePath
        if (-not (Test-Path -LiteralPath $target -PathType Container)) {
            throw "A required protected RadioTEDU data directory is missing."
        }
    }
}
else {
    New-Item -ItemType Directory -Path $OnAirRoot -Force | Out-Null
    foreach ($relativePath in $protectedRelativePaths) {
        $target = Join-Path $OnAirRoot $relativePath
        New-Item -ItemType Directory -Path $target -Force | Out-Null
    }
}

$rootItem = Get-Item -LiteralPath $OnAirRoot -Force
$directoryTargets = @($rootItem) + @(
    Get-ChildItem -LiteralPath $OnAirRoot -Directory -Recurse -Force |
        Where-Object { -not ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) }
)
$fileTargets = @(
    Get-ChildItem -LiteralPath $OnAirRoot -File -Recurse -Force |
        Where-Object { -not ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) }
)

foreach ($directory in $directoryTargets) {
    $target = $directory.FullName
    if (-not $VerifyOnly) {
        $acl = New-StrictDirectoryAcl
        $directory.SetAccessControl($acl)
    }
    Assert-StrictAcl -Target $target
}

foreach ($file in $fileTargets) {
    $target = $file.FullName
    if (-not $VerifyOnly) {
        $acl = New-StrictFileAcl
        $file.SetAccessControl($acl)
    }
    Assert-StrictAcl -Target $target
}
