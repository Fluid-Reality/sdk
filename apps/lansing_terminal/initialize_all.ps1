#Requires -Version 5.1

<#
.SYNOPSIS
Detects Lansing actuators and initializes them to a target current delta.

.DESCRIPTION
Runs lansing_terminal in JSON mode for each requested actuator. Every actuator
is detected first. If its measured current delta is above TargetDeltaMa, the
script repeatedly runs:

    detect <actuator>; initialize <actuator>

This continues even after the actuator enters Ready state, until its current
delta reaches the requested target. The post-initialization current delta must
decrease after every attempt. The script stops work on an actuator and reports
it when the delta remains the same or increases. A configurable maximum attempt
count provides an additional safety bound.

By default, actuators 0 through 23 are processed. The script turns the PSU and
PSU connection on as needed and turns both off in a finally block. Use
-LeavePowerOn only when a supervised workflow intentionally requires the board
to remain powered afterward.

.PARAMETER Port
Serial port used by the Lansing controller, such as COM6, /dev/ttyACM0, or
/dev/cu.usbmodem1101.

.PARAMETER TargetDeltaMa
Target baseline-to-forward current delta in milliamps. The allowed range is
0.1 through 3.0 mA: below 0.1 mA is classified as Not connected, while above
3.0 mA remains an Error under the SDK detection thresholds.

.PARAMETER Actuators
Actuator indices to process. Defaults to all supported indices, 0 through 23.

.PARAMETER PythonExecutable
Python executable used to run lansing_terminal.py. Defaults to python.

.PARAMETER TerminalPath
Path to the Lansing terminal lansing_terminal.py. Defaults to that file beside
this script.

.PARAMETER MaxInitializationAttempts
Maximum initialization attempts for one actuator. Defaults to 10. This bound
prevents an indefinitely decreasing delta from causing an unlimited recovery
sequence.

.PARAMETER MinimumDeltaImprovementMa
Minimum required decrease in delta, in milliamps, between initialization
attempts. Defaults to 0, meaning any strict decrease counts as progress. Set a
small positive value to reject changes that are within measurement noise.

.PARAMETER LeavePowerOn
Do not turn the PSU connection and PSU off when the script finishes. The
default is to shut both off, including after errors or interruption.

.EXAMPLE
.\initialize_all.ps1 -Port COM6 -TargetDeltaMa 1.5

Detect all 24 actuators and initialize actuators whose delta is above the
target. Shut down the PSU connection and PSU afterward.

.EXAMPLE
.\initialize_all.ps1 -Port COM6 -TargetDeltaMa 1.5 -Actuators 0,1,2,3,4,5,6,7

Process only the standard group-0 actuator range.

.EXAMPLE
.\initialize_all.ps1 -Port COM6 -TargetDeltaMa 1.5 -Actuators 0 `
    -MaxInitializationAttempts 4 `
    -MinimumDeltaImprovementMa 0.05

Process actuator 0, allow at most four initialization attempts, and require its
delta to decrease by more than 0.05 mA after each attempt.

.OUTPUTS
A formatted summary is written for the operator. The script exits with status
0 when every connected actuator reaches the target delta and no command fails.
It exits with status 1 when an actuator stalls, remains above target at the
attempt limit, or a terminal command fails. Not-connected actuators are
reported and skipped; they do not by themselves make the script fail.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateNotNullOrEmpty()]
    [string]$Port,

    [Parameter(Mandatory = $true, Position = 1)]
    [ValidateRange(0.1, 3.0)]
    [double]$TargetDeltaMa,

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$PythonExecutable = "python",

    [Parameter()]
    [string]$TerminalPath,

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [int[]]$Actuators = (0..23),

    [Parameter()]
    [ValidateRange(1, 100)]
    [int]$MaxInitializationAttempts = 10,

    [Parameter()]
    [ValidateRange(0.0, 1000.0)]
    [double]$MinimumDeltaImprovementMa = 0.0,

    [Parameter()]
    [switch]$LeavePowerOn
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($TerminalPath)) {
    $TerminalPath = Join-Path -Path $PSScriptRoot -ChildPath "lansing_terminal.py"
}
$TerminalPath = [System.IO.Path]::GetFullPath($TerminalPath)

if (-not (Test-Path -LiteralPath $TerminalPath -PathType Leaf)) {
    throw "Lansing terminal was not found at '$TerminalPath'."
}

if ($null -eq (Get-Command -Name $PythonExecutable -ErrorAction SilentlyContinue)) {
    throw "Python executable '$PythonExecutable' was not found."
}

$uniqueActuators = @($Actuators | Sort-Object -Unique)
foreach ($actuator in $uniqueActuators) {
    if ($actuator -lt 0 -or $actuator -gt 23) {
        throw "Actuator must be in the range 0..23; received $actuator."
    }
}

function Test-JsonProperty {
    param(
        [Parameter(Mandatory = $true)]
        [object]$InputObject,

        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    return $null -ne $InputObject.PSObject.Properties[$Name]
}

function Invoke-LansingTerminal {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,

        [Parameter()]
        [switch]$SuppressProgress
    )

    $records = New-Object System.Collections.Generic.List[object]
    $arguments = @(
        $TerminalPath,
        "-j",
        "--port",
        $Port,
        "-c",
        $Command
    )

    Write-Verbose "Running: $PythonExecutable $TerminalPath -j --port $Port -c `"$Command`""

    & $PythonExecutable @arguments 2>&1 | ForEach-Object {
        $line = [string]$_
        if ([string]::IsNullOrWhiteSpace($line)) {
            return
        }

        try {
            $record = $line | ConvertFrom-Json -ErrorAction Stop
            [void]$records.Add($record)

            if (
                -not $SuppressProgress -and
                (Test-JsonProperty -InputObject $record -Name "event") -and
                $record.event -eq "initialization_progress"
            ) {
                $total = [double]$record.total_s
                $elapsed = [double]$record.elapsed_s
                $percent = if ($total -gt 0) {
                    [Math]::Min(100.0, 100.0 * $elapsed / $total)
                }
                else {
                    0.0
                }
                Write-Progress `
                    -Id 1 `
                    -Activity "Initializing actuator $($record.actuator)" `
                    -Status "Stage $($record.stage)/$($record.stage_count), $([Math]::Round($elapsed, 1))/$total s" `
                    -PercentComplete $percent
            }
        }
        catch {
            Write-Warning "Non-JSON terminal output: $line"
        }
    }

    $commandExitCode = $LASTEXITCODE
    Write-Progress -Id 1 -Activity "Initializing actuator" -Completed

    return [pscustomobject]@{
        ExitCode = $commandExitCode
        Records  = $records.ToArray()
        Command  = $Command
    }
}

function Get-LastDetection {
    param(
        [Parameter(Mandatory = $true)]
        [object[]]$Records,

        [Parameter(Mandatory = $true)]
        [int]$Actuator
    )

    $detections = @(
        $Records | Where-Object {
            (Test-JsonProperty -InputObject $_ -Name "actuator") -and
            (Test-JsonProperty -InputObject $_ -Name "state") -and
            (Test-JsonProperty -InputObject $_ -Name "delta_ma") -and
            [int]$_.actuator -eq $Actuator
        }
    )

    if ($detections.Count -eq 0) {
        return $null
    }
    return $detections[-1]
}

function Get-TerminalErrorMessage {
    param(
        [Parameter(Mandatory = $true)]
        [object[]]$Records
    )

    $errors = @(
        $Records | Where-Object {
            (Test-JsonProperty -InputObject $_ -Name "event") -and
            $_.event -eq "error"
        }
    )
    if ($errors.Count -eq 0) {
        return "The terminal command failed without a structured error message."
    }
    return [string]$errors[-1].message
}

$results = New-Object System.Collections.Generic.List[object]
$scriptFailed = $false
$powerMayBeOn = $false

try {
    foreach ($actuator in $uniqueActuators) {
        Write-Host ""
        Write-Host "Actuator ${actuator}: detecting..." -ForegroundColor Cyan
        $powerMayBeOn = $true

        $detectionRun = Invoke-LansingTerminal `
            -Command "psu on; psuc on; detect $actuator" `
            -SuppressProgress

        if ($detectionRun.ExitCode -ne 0) {
            $message = Get-TerminalErrorMessage -Records $detectionRun.Records
            Write-Host "Actuator ${actuator}: detection failed: $message" -ForegroundColor Red
            [void]$results.Add([pscustomobject]@{
                Actuator = $actuator
                Status   = "Command failed"
                DeltaMa  = $null
                Attempts = 0
                Detail   = $message
            })
            $scriptFailed = $true
            continue
        }

        $detection = Get-LastDetection `
            -Records $detectionRun.Records `
            -Actuator $actuator

        if ($null -eq $detection) {
            $message = "Detection completed without an actuator result."
            Write-Host "Actuator ${actuator}: $message" -ForegroundColor Red
            [void]$results.Add([pscustomobject]@{
                Actuator = $actuator
                Status   = "No result"
                DeltaMa  = $null
                Attempts = 0
                Detail   = $message
            })
            $scriptFailed = $true
            continue
        }

        $state = [string]$detection.state
        $previousDelta = [double]$detection.delta_ma
        Write-Host "Actuator ${actuator}: $state, delta $($previousDelta.ToString('F3')) mA"

        if ($state -eq "Ready" -and $previousDelta -le $TargetDeltaMa) {
            Write-Host (
                "Actuator {0}: target reached ({1:F3} mA <= {2:F3} mA)." -f
                $actuator,
                $previousDelta,
                $TargetDeltaMa
            ) -ForegroundColor Green
            [void]$results.Add([pscustomobject]@{
                Actuator = $actuator
                Status   = "Target reached"
                DeltaMa  = $previousDelta
                Attempts = 0
                Detail   = "Target reached on initial detection"
            })
            continue
        }

        if ($state -eq "Not connected") {
            Write-Host "Actuator ${actuator}: not connected; skipping." -ForegroundColor Yellow
            [void]$results.Add([pscustomobject]@{
                Actuator = $actuator
                Status   = "Not connected"
                DeltaMa  = $previousDelta
                Attempts = 0
                Detail   = "No initialization attempted"
            })
            continue
        }

        if ($state -notin @("Ready", "Error")) {
            $message = "Unexpected actuator state '$state'."
            Write-Host "Actuator ${actuator}: $message" -ForegroundColor Red
            [void]$results.Add([pscustomobject]@{
                Actuator = $actuator
                Status   = "Unexpected state"
                DeltaMa  = $previousDelta
                Attempts = 0
                Detail   = $message
            })
            $scriptFailed = $true
            continue
        }

        if ($state -eq "Ready") {
            Write-Host (
                "Actuator {0}: Ready, but delta {1:F3} mA is above target {2:F3} mA; continuing initialization." -f
                $actuator,
                $previousDelta,
                $TargetDeltaMa
            ) -ForegroundColor Yellow
        }

        $resolved = $false
        for ($attempt = 1; $attempt -le $MaxInitializationAttempts; $attempt++) {
            Write-Host (
                "Actuator {0}: initialization attempt {1}/{2}; previous delta {3:F3} mA" -f
                $actuator,
                $attempt,
                $MaxInitializationAttempts,
                $previousDelta
            ) -ForegroundColor Yellow

            # Detection and initialization must run in the same terminal process.
            # A new SDK object begins with Unknown actuator state, and initialize
            # intentionally requires a successful detection first.
            $initializationRun = Invoke-LansingTerminal `
                -Command "psu on; psuc on; detect $actuator; initialize $actuator"

            if ($initializationRun.ExitCode -ne 0) {
                $message = Get-TerminalErrorMessage -Records $initializationRun.Records
                Write-Host "Actuator ${actuator}: initialization failed: $message" -ForegroundColor Red
                [void]$results.Add([pscustomobject]@{
                    Actuator = $actuator
                    Status   = "Command failed"
                    DeltaMa  = $previousDelta
                    Attempts = $attempt
                    Detail   = $message
                })
                $scriptFailed = $true
                $resolved = $true
                break
            }

            $after = Get-LastDetection `
                -Records $initializationRun.Records `
                -Actuator $actuator

            if ($null -eq $after) {
                $message = "Initialization completed without a final detection result."
                Write-Host "Actuator ${actuator}: $message" -ForegroundColor Red
                [void]$results.Add([pscustomobject]@{
                    Actuator = $actuator
                    Status   = "No result"
                    DeltaMa  = $previousDelta
                    Attempts = $attempt
                    Detail   = $message
                })
                $scriptFailed = $true
                $resolved = $true
                break
            }

            $newState = [string]$after.state
            $newDelta = [double]$after.delta_ma
            $improvement = $previousDelta - $newDelta

            Write-Host (
                "Actuator {0}: {1}, delta {2:F3} mA, improvement {3:F3} mA" -f
                $actuator,
                $newState,
                $newDelta,
                $improvement
            )

            if ($newState -eq "Ready" -and $newDelta -le $TargetDeltaMa) {
                Write-Host (
                    "Actuator {0}: target reached ({1:F3} mA <= {2:F3} mA)." -f
                    $actuator,
                    $newDelta,
                    $TargetDeltaMa
                ) -ForegroundColor Green
                [void]$results.Add([pscustomobject]@{
                    Actuator = $actuator
                    Status   = "Target reached"
                    DeltaMa  = $newDelta
                    Attempts = $attempt
                    Detail   = "Target delta reached"
                })
                $resolved = $true
                break
            }

            if ($newState -eq "Not connected") {
                Write-Host "Actuator ${actuator}: now reports not connected; stopping." -ForegroundColor Red
                [void]$results.Add([pscustomobject]@{
                    Actuator = $actuator
                    Status   = "Not connected"
                    DeltaMa  = $newDelta
                    Attempts = $attempt
                    Detail   = "State changed to Not connected after initialization"
                })
                $scriptFailed = $true
                $resolved = $true
                break
            }

            if ($newState -notin @("Ready", "Error")) {
                $message = "Unexpected post-initialization state '$newState'."
                Write-Host "Actuator ${actuator}: $message" -ForegroundColor Red
                [void]$results.Add([pscustomobject]@{
                    Actuator = $actuator
                    Status   = "Unexpected state"
                    DeltaMa  = $newDelta
                    Attempts = $attempt
                    Detail   = $message
                })
                $scriptFailed = $true
                $resolved = $true
                break
            }

            if ($improvement -le $MinimumDeltaImprovementMa) {
                $message = (
                    "Delta stopped decreasing: {0:F3} mA -> {1:F3} mA " +
                    "(required improvement > {2:F3} mA)."
                ) -f $previousDelta, $newDelta, $MinimumDeltaImprovementMa
                Write-Host "Actuator ${actuator}: $message" -ForegroundColor Red
                [void]$results.Add([pscustomobject]@{
                    Actuator = $actuator
                    Status   = "Stalled"
                    DeltaMa  = $newDelta
                    Attempts = $attempt
                    Detail   = $message
                })
                $scriptFailed = $true
                $resolved = $true
                break
            }

            $previousDelta = $newDelta
        }

        if (-not $resolved) {
            $message = (
                "Still above target {0:F3} mA after {1} initialization attempts; " +
                "last delta {2:F3} mA."
            ) -f $TargetDeltaMa, $MaxInitializationAttempts, $previousDelta
            Write-Host "Actuator ${actuator}: $message" -ForegroundColor Red
            [void]$results.Add([pscustomobject]@{
                Actuator = $actuator
                Status   = "Above target"
                DeltaMa  = $previousDelta
                Attempts = $MaxInitializationAttempts
                Detail   = $message
            })
            $scriptFailed = $true
        }
    }
}
finally {
    Write-Progress -Id 1 -Activity "Initializing actuator" -Completed
    if ($powerMayBeOn -and -not $LeavePowerOn) {
        Write-Host ""
        Write-Host "Turning the PSU connection off, then turning the PSU off..." -ForegroundColor Cyan
        try {
            $shutdown = Invoke-LansingTerminal `
                -Command "psuc off; psu off" `
                -SuppressProgress
            if ($shutdown.ExitCode -ne 0) {
                $message = Get-TerminalErrorMessage -Records $shutdown.Records
                Write-Warning "Automatic power shutdown failed: $message"
                $scriptFailed = $true
            }
        }
        catch {
            Write-Warning "Automatic power shutdown failed: $($_.Exception.Message)"
            $scriptFailed = $true
        }
    }
}

Write-Host ""
Write-Host "Initialization summary" -ForegroundColor Cyan
$results | Format-Table Actuator, Status, DeltaMa, Attempts, Detail -AutoSize | Out-Host

if ($LeavePowerOn) {
    Write-Warning "The PSU connection and PSU were intentionally left on."
}

if ($scriptFailed) {
    exit 1
}
exit 0
