<#
AI status LED - CodeBuddy autostart watcher (portable)

This file lives in <APP>\tools\. <APP> is derived from the script's own
location, so copying the whole folder to another PC just works: there are
no hardcoded machine paths.

Usage (current user, NO administrator rights required):
  Install   : powershell -ExecutionPolicy Bypass -File .\tools\codebuddy-autostart.ps1 -Install
  Uninstall : powershell -ExecutionPolicy Bypass -File .\tools\codebuddy-autostart.ps1 -Uninstall
  Run once  : powershell -ExecutionPolicy Bypass -File .\tools\codebuddy-autostart.ps1

After install, a hidden watcher starts at every logon. When CodeBuddy opens,
it makes sure start.bat is running. It probes /health first, so it never
starts a second copy of the service. If the service dies while CodeBuddy is
open, it is restarted at most once every 30 seconds.

Note: all messages here are ASCII on purpose - Windows PowerShell 5.1 reads
.ps1 files as ANSI, so non-ASCII text would be corrupted.
#>
param(
    [switch]$Install,
    [switch]$Uninstall
)

$ErrorActionPreference = 'SilentlyContinue'

$AppDir   = Split-Path -Parent $PSScriptRoot   # tools/ -> <APP>
$StartBat = Join-Path $AppDir 'start.bat'
$Port     = 8765
$PollSec  = 3
$RunKey   = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$RunName  = 'AIStatusLEDWatcher'
$LogDir   = Join-Path $env:USERPROFILE '.codebuddy'
$LogFile  = Join-Path $LogDir 'aistatus-watcher.log'
$Exe      = 'powershell.exe'
$ArgsLine = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $PSCommandPath

function Write-Log([string]$Text) {
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
    $line = '{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Text
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

function Get-WatcherProcess {
    # Only the resident watcher counts. The -Install/-Uninstall process also
    # has this script name in its command line, so it must be excluded -
    # otherwise Start-Watcher would think a watcher is already running.
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'powershell.exe' -and
        $_.CommandLine -match 'codebuddy-autostart\.ps1' -and
        $_.CommandLine -match '-WindowStyle Hidden' -and
        $_.CommandLine -notmatch '-Install' -and
        $_.CommandLine -notmatch '-Uninstall'
    })
}

function Get-CBProcess {
    @(Get-Process | Where-Object { $_.ProcessName -match '^CodeBuddy' })
}

function Test-ServiceAlive {
    try {
        $r = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/health" -f $Port) -TimeoutSec 1
        return [bool]$r.ok
    } catch {
        return $false
    }
}

function Start-Watcher {
    if ((Get-WatcherProcess).Count -eq 0) {
        Start-Process -FilePath $Exe -ArgumentList $ArgsLine -WindowStyle Hidden
        Start-Sleep -Seconds 1
    }
}

if ($Install) {
    if (-not (Test-Path $StartBat)) {
        Write-Host ('ERROR: start.bat not found: ' + $StartBat)
        exit 1
    }
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
    New-ItemProperty -Path $RunKey -Name $RunName -Value ($Exe + ' ' + $ArgsLine) `
        -PropertyType String -Force | Out-Null
    Start-Watcher
    Write-Host 'Installed. Autostart: HKCU Run / AIStatusLEDWatcher (logon, current user).'
    Write-Host ('Watch script : ' + $PSCommandPath)
    Write-Host ('start.bat    : ' + $StartBat)
    Write-Host ('Log file     : ' + $LogFile)
    exit 0
}

if ($Uninstall) {
    Remove-ItemProperty -Path $RunKey -Name $RunName -ErrorAction SilentlyContinue
    Get-WatcherProcess | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host 'Uninstalled. The LED service itself was NOT stopped.'
    exit 0
}

function Ensure-StatusService {
    if (-not (Test-Path $StartBat)) {
        Write-Log ('ERROR start.bat not found: ' + $StartBat)
        return
    }
    if (Test-ServiceAlive) {
        Write-Log 'Service already running, skip.'
        return
    }
    Start-Process -FilePath 'cmd.exe' `
        -ArgumentList '/c', ('"{0}"' -f $StartBat) `
        -WorkingDirectory $AppDir -WindowStyle Hidden
    Write-Log ('Started: ' + $StartBat)
}

# keep the log small
$log = Get-Item $LogFile -ErrorAction SilentlyContinue
if ($log -and $log.Length -gt 1MB) { Set-Content -Path $LogFile -Value '' -Encoding UTF8 }

Write-Log ('Watcher started. AppDir=' + $AppDir)
$wasRunning = $false
$lastStart  = [datetime]::MinValue

while ($true) {
    Start-Sleep -Seconds $PollSec
    $isRunning  = (Get-CBProcess).Count -gt 0
    $justOpened = $isRunning -and -not $wasRunning

    if ($justOpened) {
        Write-Log 'CodeBuddy opened -> ensure status LED service.'
        Ensure-StatusService
        $lastStart = Get-Date
    } elseif ($isRunning -and -not (Test-ServiceAlive)) {
        # service crashed / was closed while CodeBuddy is still open
        if (((Get-Date) - $lastStart).TotalSeconds -ge 30) {
            Write-Log 'Service unreachable while CodeBuddy is open -> restart once.'
            Ensure-StatusService
            $lastStart = Get-Date
        }
    }
    $wasRunning = $isRunning
}
