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
    [Parameter(Mandatory = $true)]
    [string]$ApiDataDir,
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

function Initialize-Utf8Log {
    if (-not (Test-Path -LiteralPath $logPath -PathType Leaf)) {
        return
    }
    $bytes = [System.IO.File]::ReadAllBytes($logPath)
    $hasUtf16Bom = $bytes.Length -ge 2 -and (
        ($bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) -or
        ($bytes[0] -eq 0xFE -and $bytes[1] -eq 0xFF)
    )
    $hasNullByte = [Array]::IndexOf($bytes, [byte]0) -ge 0
    $invalidUtf8 = $false
    try {
        $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
        $null = $strictUtf8.GetString($bytes)
    }
    catch {
        $invalidUtf8 = $true
    }
    if ($hasUtf16Bom -or $hasNullByte -or $invalidUtf8) {
        # Preserve the old diagnostic without appending another encoding to it.
        Move-Item -LiteralPath $logPath -Destination ($logPath + '.mixed-encoding') -Force
    }
}

Initialize-Utf8Log

function Write-MonitorLog([string]$Level, [string]$Message) {
    if ((Test-Path -LiteralPath $logPath -PathType Leaf) -and
        (Get-Item -LiteralPath $logPath).Length -gt 5MB) {
        Move-Item -LiteralPath $logPath -Destination ($logPath + '.1') -Force
    }
    $timestamp = [DateTimeOffset]::UtcNow.ToString('o')
    Add-Content -LiteralPath $logPath -Value "$timestamp [$Level] $Message" -Encoding UTF8
}

function Write-RuntimeJson([object]$Record) {
    $line = [string]$Record
    if ($line.Length -gt 4096) {
        Write-MonitorLog 'WARN' 'runtime_output_redacted reason=oversized'
        return
    }
    try {
        $null = ConvertFrom-Json -InputObject $line -ErrorAction Stop
        Write-MonitorLog 'RUNTIME' $line
    }
    catch {
        # Native startup failures can contain terminal paths or environment
        # details. Keep the log useful without persisting that raw diagnostic.
        Write-MonitorLog 'WARN' 'runtime_output_redacted reason=non_json'
    }
}

if (Test-Path -LiteralPath $stopPath -PathType Leaf) {
    Write-MonitorLog 'INFO' 'quote_monitor_not_started stop_file_detected=true'
    exit 0
}

$arguments = @(
    '-m', 'fixedstep_monitor', 'quote-live',
    '--interval', [string]$IntervalSeconds,
    '--publish-interval', [string]$PublishIntervalSeconds,
    '--source-stale-after', [string]([Math]::Max(45, $IntervalSeconds * 3)),
    '--database', $DatabasePath,
    '--site-root', $SiteRoot,
    '--api-data-dir', $ApiDataDir,
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
    & $resolvedPython @arguments 2>&1 | ForEach-Object {
        Write-RuntimeJson $_
    }
    $exitCode = $LASTEXITCODE
}
finally {
    $env:FIXEDSTEP_MONITOR_GIT_TIMEOUT_SECONDS = $priorGitTimeout
}
Write-MonitorLog 'INFO' "quote_monitor_exited code=$exitCode"
exit $exitCode
