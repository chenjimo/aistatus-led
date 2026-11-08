<#
AI status LED - multi-app autostart watcher (portable)

Watches CodeBuddy, OpenAI Codex CLI and DeepSeek (DSH). When ANY of them is
running, it makes sure the LED middleware (start.bat -> main.py start) is up.
It probes /health first, so it never starts a second copy. If the service dies
while an app is open, it restarts at most once every 30 seconds.

This file lives in <APP>\tools\. <APP> is derived from the script's own
location, so copying the whole folder to another PC just works.

Usage (current user, NO administrator rights required):
  Install   : powershell -ExecutionPolicy Bypass -File .\tools\codebuddy-autostart.ps1 -Install
  Uninstall : powershell -ExecutionPolicy Bypass -File .\tools\codebuddy-autostart.ps1 -Uninstall
  Stop now  : powershell -ExecutionPolicy Bypass -File .\tools\codebuddy-autostart.ps1 -Stop
  Status    : powershell -ExecutionPolicy Bypass -File .\tools\codebuddy-autostart.ps1 -Status
  Install + stop service when all apps close:
              powershell -ExecutionPolicy Bypass -File .\tools\codebuddy-autostart.ps1 -Install -StopWhenAllClosed

Note: all messages here are ASCII on purpose - Windows PowerShell 5.1 reads
.ps1 files as ANSI, so non-ASCII text would be corrupted.
#>
param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Stop,
    [switch]$Status,
    [switch]$StopWhenAllClosed
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

# Apps that should trigger the LED service. Matched against process name
# (fast) and command line (covers node-launched CLIs like codex / dsh).
$AppNamePatterns = @('^CodeBuddy', '^codex', '^dsh', '^deepseek')
$AppCmdPatterns  = @('codex\.js', 'codex\.exe', '\\codex\b', 'deepseek-harness',
                     'dsh\.js', 'dsh\.exe', '\\dsh\b')

$ExtraArgs = ''
if ($StopWhenAllClosed) { $ExtraArgs = ' -StopWhenAllClosed' }
$ArgsLine = ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"{1}' -f $PSCommandPath, $ExtraArgs)

function Write-Log([string]$Text) {
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
    $line = '{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Text
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

function Get-WatcherProcess {
    # Only the resident watcher counts. The -Install/-Uninstall/-Stop/-Status
    # process also has this script name in its command line, so it is excluded.
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'powershell.exe' -and
        $_.CommandLine -match 'codebuddy-autostart\.ps1' -and
        $_.CommandLine -match '-WindowStyle Hidden' -and
        $_.CommandLine -notmatch '-(Install|Uninstall|Stop|Status)\b'
    })
}

function Get-AppProcess {
    @(Get-CimInstance Win32_Process | Where-Object {
        $n = [string]$_.Name
        $c = [string]$_.CommandLine
        $hit = $false
        foreach ($p in $AppNamePatterns) { if ($n -match $p) { $hit = $true; break } }
        if (-not $hit) {
            foreach ($p in $AppCmdPatterns) { if ($c -match $p) { $hit = $true; break } }
        }
        $hit
    })
}

function Test-ServiceAlive {
    try {
        $r = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/health" -f $Port) -TimeoutSec 3
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

function Stop-StatusService {
    Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -match 'main\.py start'
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Show-Status {
    $svc = Test-ServiceAlive
    $watchers = (Get-WatcherProcess).Count
    $apps = Get-AppProcess
    Write-Host ('service_alive = ' + $svc)
    Write-Host ('watcher_count = ' + $watchers)
    Write-Host ('watched_apps  = ' + (($apps | ForEach-Object { $_.Name } | Sort-Object -Unique) -join ','))
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
    Write-Host ('Watching     : CodeBuddy / Codex / DSH')
    exit 0
}

if ($Uninstall) {
    Remove-ItemProperty -Path $RunKey -Name $RunName -ErrorAction SilentlyContinue
    Get-WatcherProcess | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host 'Uninstalled. Autostart removed, watcher stopped. The LED service itself was NOT stopped.'
    Write-Host 'Use -Stop to stop the LED service too.'
    exit 0
}

if ($Stop) {
    Stop-StatusService
    Write-Host 'LED service stopped (main.py start). Watcher left as-is.'
    exit 0
}

if ($Status) {
    Show-Status
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
        -ArgumentList '/c', ('"{0}" --no-elevate' -f $StartBat) `
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
    $procs = Get-AppProcess
    $isRunning = $procs.Count -gt 0
    $justOpened = $isRunning -and -not $wasRunning

    if ($justOpened) {
        $names = (($procs | ForEach-Object { $_.Name } | Sort-Object -Unique) -join ',')
        Write-Log ('App opened (' + $names + ') -> ensure status LED service.')
        Ensure-StatusService
        $lastStart = Get-Date
    } elseif ($isRunning -and -not (Test-ServiceAlive)) {
        # service crashed / was closed while an app is still open
        if (((Get-Date) - $lastStart).TotalSeconds -ge 30) {
            Write-Log 'Service unreachable while an app is open -> restart once.'
            Ensure-StatusService
            $lastStart = Get-Date
        }
    } elseif (-not $isRunning -and $wasRunning) {
        Write-Log 'All watched apps closed.'
        if ($StopWhenAllClosed) {
            Stop-StatusService
            Write-Log 'StopWhenAllClosed: LED service stopped.'
        }
    }
    $wasRunning = $isRunning
}
