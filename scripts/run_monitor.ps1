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
    [string]$StrategyStatePath,
    [ValidateRange(30, 60)]
    [int]$IntervalSeconds = 60,
    [ValidateRange(600, 86400)]
    [int]$PublishIntervalSeconds = 600,
    [ValidateRange(1, 600)]
    [int]$GitTimeoutSeconds = 120,
    [string[]]$Symbols = @('FixedStep0.5'),
    [long[]]$MagicNumbers = @(),
    [Nullable[double]]$InitialCapital,
    [string]$PublicBaseUrl = 'https://mahvro.github.io/fixedstep-monitor',
    [switch]$EnableGitPublish,
    [string]$GitRemote = 'origin',
    [string]$GitBranch = 'live'
)

$ErrorActionPreference = 'Stop'
$resolvedProject = (Resolve-Path -LiteralPath $ProjectRoot).Path
$resolvedPython = (Resolve-Path -LiteralPath $PythonExe).Path
$runtimeDirectory = Join-Path $resolvedProject 'runtime'
$logPath = Join-Path $runtimeDirectory 'monitor.log'
$stopPath = Join-Path $runtimeDirectory 'stop-monitor'
New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null

function Write-MonitorLog([string]$Level, [string]$Message) {
    if ((Test-Path -LiteralPath $logPath -PathType Leaf) -and
        (Get-Item -LiteralPath $logPath).Length -gt 5MB) {
        Move-Item -LiteralPath $logPath -Destination ($logPath + '.1') -Force
    }
    $timestamp = [DateTimeOffset]::UtcNow.ToString('o')
    Add-Content -LiteralPath $logPath -Value "$timestamp [$Level] $Message" -Encoding UTF8
}

$lastPublishAttempt = [DateTimeOffset]::MinValue
Write-MonitorLog 'INFO' "monitor_started collection_interval=$IntervalSeconds publish_enabled=$($EnableGitPublish.IsPresent)"

while (-not (Test-Path -LiteralPath $stopPath)) {
    $cycleStarted = [DateTimeOffset]::UtcNow
    try {
        $collectArguments = @(
            '-m', 'fixedstep_monitor', 'collect',
            '--interval', [string]$IntervalSeconds,
            '--database', $DatabasePath,
            '--site-root', $SiteRoot,
            '--public-base-url', $PublicBaseUrl
        )
        foreach ($symbol in $Symbols) {
            $collectArguments += @('--symbol', $symbol)
        }
        foreach ($magic in $MagicNumbers) {
            $collectArguments += @('--magic', [string]$magic)
        }
        if ($StrategyStatePath) {
            $collectArguments += @('--strategy-state', $StrategyStatePath)
        }
        if ($null -ne $InitialCapital) {
            $collectArguments += @('--initial-capital', [string]$InitialCapital.Value)
        }

        $collectionOutput = (& $resolvedPython @collectArguments 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) {
            throw "collector_exit=$LASTEXITCODE"
        }
        Write-MonitorLog 'INFO' "collection_complete $collectionOutput"
    }
    catch {
        Write-MonitorLog 'ERROR' "collection_failed $($_.Exception.Message)"
    }

    $publishDue = ([DateTimeOffset]::UtcNow - $lastPublishAttempt).TotalSeconds -ge $PublishIntervalSeconds
    if ($EnableGitPublish -and $publishDue) {
        # Advance the attempt clock before publication so failures cannot cause a
        # retry on every collection cycle.
        $lastPublishAttempt = [DateTimeOffset]::UtcNow
        try {
            $publishArguments = @(
                '-m', 'fixedstep_monitor', 'publish',
                '--repository', $resolvedProject,
                '--generated-path', 'docs/ai',
                '--commit', '--push', '--rolling-snapshot',
                '--remote', $GitRemote,
                '--branch', $GitBranch
            )
            $priorGitTimeout = $env:FIXEDSTEP_MONITOR_GIT_TIMEOUT_SECONDS
            $env:FIXEDSTEP_MONITOR_GIT_TIMEOUT_SECONDS = [string]$GitTimeoutSeconds
            try {
                $publishOutput = (& $resolvedPython @publishArguments 2>&1 | Out-String).Trim()
            }
            finally {
                $env:FIXEDSTEP_MONITOR_GIT_TIMEOUT_SECONDS = $priorGitTimeout
            }
            if ($LASTEXITCODE -ne 0) {
                throw "publisher_exit=$LASTEXITCODE"
            }
            Write-MonitorLog 'INFO' "publication_complete $publishOutput"
        }
        catch {
            Write-MonitorLog 'ERROR' "publication_failed $($_.Exception.Message)"
        }
    }

    $elapsedSeconds = ([DateTimeOffset]::UtcNow - $cycleStarted).TotalSeconds
    $sleepSeconds = [Math]::Max(1, [Math]::Ceiling($IntervalSeconds - $elapsedSeconds))
    Start-Sleep -Seconds $sleepSeconds
}

Write-MonitorLog 'INFO' 'monitor_stopped stop_file_detected=true'
