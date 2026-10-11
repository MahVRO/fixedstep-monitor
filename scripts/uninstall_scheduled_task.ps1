[CmdletBinding()]
param(
    [string]$TaskName = 'FixedStep0.5 Read-Only Monitor'
)

$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    [pscustomobject]@{ TaskName = $TaskName; Removed = $false; Reason = 'not_found' }
    return
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
[pscustomobject]@{ TaskName = $TaskName; Removed = $true }
