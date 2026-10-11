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
    [ValidateRange(10, 60)]
    [int]$IntervalSeconds = 10,
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
    [string]$GitBranch = 'live',
    [string]$RealtimeIngestUrl,
    [string]$RealtimeTokenPath,
    [ValidateRange(1, 10)]
    [int]$RealtimeTimeoutSeconds = 5
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

$realtimeToken = $null
$realtimeBstr = [IntPtr]::Zero
if ($RealtimeIngestUrl) {
    try {
        if ($IntervalSeconds -ne 10) {
            throw 'unsupported realtime interval'
        }
        if (-not $RealtimeTokenPath -or
            -not (Test-Path -LiteralPath $RealtimeTokenPath -PathType Leaf)) {
            throw 'missing protected token'
        }
        $protectedToken = Get-Content -LiteralPath $RealtimeTokenPath -Raw -ErrorAction Stop
        $secureToken = ConvertTo-SecureString -String $protectedToken -ErrorAction Stop
        $realtimeBstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
        $realtimeToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($realtimeBstr)
        if ($realtimeToken.Length -lt 32 -or
            $realtimeToken.Length -gt 256 -or
            $realtimeToken -notmatch '^[\x21-\x7E]+$') {
            throw 'invalid protected token'
        }
        $arguments += @(
            '--realtime-ingest-url', $RealtimeIngestUrl,
            '--realtime-timeout', [string]$RealtimeTimeoutSeconds
        )
    }
    catch {
        # A relay credential problem must not stop local quote collection or
        # the static GitHub fallback. Never log the path or exception text.
        $realtimeToken = $null
        Write-MonitorLog 'WARN' 'realtime_publish_disabled category=ProtectedTokenUnavailable'
    }
}

Write-MonitorLog 'INFO' "quote_monitor_started collection_interval=$IntervalSeconds publish_enabled=$($EnableGitPublish.IsPresent)"
$priorGitTimeout = $env:FIXEDSTEP_MONITOR_GIT_TIMEOUT_SECONDS
$priorRealtimeToken = $env:FIXEDSTEP_REALTIME_TOKEN
$priorErrorActionPreference = $ErrorActionPreference
$env:FIXEDSTEP_MONITOR_GIT_TIMEOUT_SECONDS = [string]$GitTimeoutSeconds
if ($realtimeToken) {
    $env:FIXEDSTEP_REALTIME_TOKEN = $realtimeToken
}
try {
    # Windows PowerShell converts native stderr into ErrorRecord objects. With
    # Stop semantics, one harmless MT5 diagnostic would terminate the wrapper
    # before Python can apply its own fail-safe collection handling.
    $ErrorActionPreference = 'Continue'
    & $resolvedPython @arguments 2>&1 | ForEach-Object {
        Write-RuntimeJson $_
    }
    if ($null -eq $LASTEXITCODE) {
        # A native invocation failure in Windows PowerShell 5.1 is not always
        # terminating under Continue semantics. Preserve Task Scheduler retry.
        Write-MonitorLog 'WARN' 'runner_pipeline_failed category=NativeInvocationFailed'
        $exitCode = 1
    }
    else {
        $exitCode = $LASTEXITCODE
    }
}
catch {
    # Never persist the exception text: startup failures can include private
    # paths or endpoints. Task Scheduler can safely retry a non-zero exit.
    Write-MonitorLog 'WARN' "runner_pipeline_failed category=$($_.Exception.GetType().Name)"
    $exitCode = 1
}
finally {
    $ErrorActionPreference = $priorErrorActionPreference
    $env:FIXEDSTEP_MONITOR_GIT_TIMEOUT_SECONDS = $priorGitTimeout
    $env:FIXEDSTEP_REALTIME_TOKEN = $priorRealtimeToken
    if ($realtimeBstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($realtimeBstr)
    }
    $realtimeToken = $null
}
Write-MonitorLog 'INFO' "quote_monitor_exited code=$exitCode"
exit $exitCode
