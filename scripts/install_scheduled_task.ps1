[CmdletBinding()]
param(
    [string]$TaskName = 'FixedStep0.5 Quote Monitor',
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,
    [string]$PythonExe,
    [string]$DatabasePath,
    [string]$SiteRoot,
    [string]$ApiDataDir,
    [ValidateRange(15, 60)]
    [int]$IntervalSeconds = 15,
    [ValidateRange(600, 86400)]
    [int]$PublishIntervalSeconds = 600,
    [ValidateSet('FixedStep0.5')]
    [string]$Symbol = 'FixedStep0.5',
    [string]$PublicBaseUrl = 'https://mahvro.github.io/fixedstep-monitor',
    [string]$ApiHost = '127.0.0.1',
    [ValidateRange(1, 65535)]
    [int]$ApiPort = 8765,
    [string]$AllowedOrigin = 'https://mahvro.github.io',
    [switch]$EnableGitPublish,
    [string]$GitRemote = 'origin',
    [string]$GitBranch = 'live'
)

$ErrorActionPreference = 'Stop'
$resolvedProject = (Resolve-Path -LiteralPath $ProjectRoot).Path

if (-not $PythonExe) {
    $PythonExe = Join-Path $resolvedProject '.venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw 'Python executable was not found. Create the project virtual environment first.'
}
if (-not $DatabasePath) {
    $DatabasePath = Join-Path $resolvedProject 'runtime\fixedstep-quotes.sqlite3'
}
if (-not $SiteRoot) {
    $SiteRoot = Join-Path $resolvedProject 'docs'
}
if (-not $ApiDataDir) {
    $ApiDataDir = Join-Path $resolvedProject 'runtime\quote-api'
}
$runner = Join-Path $resolvedProject 'scripts\run_monitor.ps1'
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw 'The quote monitor runner script was not found.'
}
if ($EnableGitPublish) {
    if (-not (Test-Path -LiteralPath (Join-Path $resolvedProject '.git') -PathType Container)) {
        throw 'Git publication requires a repository checkout.'
    }
    $currentBranch = (& git -C $resolvedProject branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0 -or $currentBranch -ne $GitBranch) {
        throw "Git publication requires the '$GitBranch' snapshot branch to be checked out."
    }
}

function Quote-TaskArgument([string]$Value) {
    return '"' + $Value.Replace('"', '\"') + '"'
}

$arguments = @(
    '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
    '-File', (Quote-TaskArgument $runner),
    '-ProjectRoot', (Quote-TaskArgument $resolvedProject),
    '-PythonExe', (Quote-TaskArgument $PythonExe),
    '-IntervalSeconds', $IntervalSeconds,
    '-PublishIntervalSeconds', $PublishIntervalSeconds,
    '-DatabasePath', (Quote-TaskArgument $DatabasePath),
    '-SiteRoot', (Quote-TaskArgument $SiteRoot),
    '-ApiDataDir', (Quote-TaskArgument $ApiDataDir),
    '-PublicBaseUrl', (Quote-TaskArgument $PublicBaseUrl),
    '-Symbol', (Quote-TaskArgument $Symbol),
    '-ApiHost', (Quote-TaskArgument $ApiHost),
    '-ApiPort', $ApiPort,
    '-AllowedOrigin', (Quote-TaskArgument $AllowedOrigin)
)
if ($EnableGitPublish) {
    $arguments += @(
        '-EnableGitPublish',
        '-GitRemote', (Quote-TaskArgument $GitRemote),
        '-GitBranch', (Quote-TaskArgument $GitBranch)
    )
}

$action = New-ScheduledTaskAction `
    -Execute (Get-Command powershell.exe).Source `
    -Argument ($arguments -join ' ') `
    -WorkingDirectory $resolvedProject
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650)

$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'Quote-only FixedStep0.5 MT5 sampling, loopback API, and sanitized static fallback. Contains no account or trade action.'
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null

$registered = Get-ScheduledTask -TaskName $TaskName
[pscustomobject]@{
    TaskName = $registered.TaskName
    State = $registered.State
    Executable = $PythonExe
    IntervalSeconds = $IntervalSeconds
    PublishEnabled = $EnableGitPublish.IsPresent
    PublishIntervalSeconds = $PublishIntervalSeconds
    DatabasePath = $DatabasePath
    ApiDataDir = $ApiDataDir
    ApiEndpoint = "http://${ApiHost}:${ApiPort}/v1/latest"
    HistoryEndpoint = "http://${ApiHost}:${ApiPort}/v1/quotes"
    SiteRoot = $SiteRoot
}
