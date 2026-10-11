[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [Security.SecureString]$Secret,
    [Parameter(Mandatory = $true)]
    [string]$Destination
)

$ErrorActionPreference = 'Stop'
$parent = Split-Path -Parent $Destination
if (-not $parent) {
    throw 'Destination must include a parent directory.'
}
New-Item -ItemType Directory -Path $parent -Force | Out-Null

$validationBstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secret)
$plainSecret = $null
try {
    $plainSecret = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($validationBstr)
    if ($plainSecret.Length -lt 32 -or
        $plainSecret.Length -gt 256 -or
        $plainSecret -notmatch '^[\x21-\x7E]+$') {
        throw 'Secret must be 32-256 visible ASCII characters.'
    }
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($validationBstr)
    $plainSecret = $null
}

# ConvertFrom-SecureString without a key uses Windows DPAPI for the current
# user. The scheduled task must run as that same user to decrypt the value.
$protected = ConvertFrom-SecureString -SecureString $Secret
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText($Destination, $protected, $utf8NoBom)

$identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
$system = New-Object Security.Principal.SecurityIdentifier('S-1-5-18')
$acl = New-Object Security.AccessControl.FileSecurity
$acl.SetAccessRuleProtection($true, $false)
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
    $identity,
    [Security.AccessControl.FileSystemRights]::FullControl,
    [Security.AccessControl.AccessControlType]::Allow
)))
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
    $system,
    [Security.AccessControl.FileSystemRights]::FullControl,
    [Security.AccessControl.AccessControlType]::Allow
)))
Set-Acl -LiteralPath $Destination -AclObject $acl

[pscustomobject]@{
    Protected = $true
    CurrentUserOnly = $true
    Destination = $Destination
}
