[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,
    [Parameter(Mandatory = $true)]
    [string]$DatabasePath,
    [Parameter(Mandatory = $true)]
    [string]$SiteRoot,
    [ValidateRange(15, 60)]
    [int]$IntervalSeconds = 15,
    [ValidateRange(600, 86400)]
    [int]$PublishIntervalSeconds = 600,
    [ValidateRange(1, 600)]
    [int]$GitTimeoutSeconds = 120,
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
$resolvedPython = (Resolve-Path -LiteralPath $PythonExe).Path
$runtimeDirectory = Join-Path $resolvedProject 'runtime'
$logPath = Join-Path $runtimeDirectory 'quote-monitor.log'
$stopPath = Join-Path $runtimeDirectory 'stop-quote-monitor'
New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null

function Write-MonitorLog([string]$Level, [string]$Message) {
    if ((Test-Path -LiteralPath $logPath -PathType Leaf) -and
        (Get-Item -LiteralPath $logPath).Length -gt 5MB) {
        Move-Item -LiteralPath $logPath -Destination ($logPath + '.1') -Force
    }
    $timestamp = [DateTimeOffset]::UtcNow.ToString('o')
    Add-Content -LiteralPath $logPath -Value "$timestamp [$Level] $Message" -Encoding UTF8
}

if (Test-Path -LiteralPath $stopPath -PathType Leaf) {
    Write-MonitorLog 'INFO' 'quote_monitor_not_started stop_file_detected=true'
    exit 0
}

$arguments = @(
    '-m', 'fixedstep_monitor', 'quote-live',
    '--interval', [string]$IntervalSeconds,
    '--publish-interval', [string]$PublishIntervalSeconds,
    '--source-stale-after', '45',
    '--database', $DatabasePath,
    '--site-root', $SiteRoot,
    '--public-base-url', $PublicBaseUrl,
    '--symbol', $Symbol,
    '--api-host', $ApiHost,
    '--api-port', [string]$ApiPort,
    '--allowed-origin', $AllowedOrigin,
    '--stop-file', $stopPath,
    '--repository', $resolvedProject,
    '--remote', $GitRemote,
    '--branch', $GitBranch
)
if ($EnableGitPublish) {
    $arguments += '--enable-git-publish'
}

Write-MonitorLog 'INFO' "quote_monitor_started collection_interval=$IntervalSeconds publish_enabled=$($EnableGitPublish.IsPresent)"
$priorGitTimeout = $env:FIXEDSTEP_MONITOR_GIT_TIMEOUT_SECONDS
$env:FIXEDSTEP_MONITOR_GIT_TIMEOUT_SECONDS = [string]$GitTimeoutSeconds
try {
    & $resolvedPython @arguments *>> $logPath
    $exitCode = $LASTEXITCODE
}
finally {
    $env:FIXEDSTEP_MONITOR_GIT_TIMEOUT_SECONDS = $priorGitTimeout
}
Write-MonitorLog 'INFO' "quote_monitor_exited code=$exitCode"
exit $exitCode
