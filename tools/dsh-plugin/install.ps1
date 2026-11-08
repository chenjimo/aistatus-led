<#
aistatus-led DSH plugin installer (insert-patch mode)

  Install   : powershell -ExecutionPolicy Bypass -File .\install.ps1
  Status    : powershell -ExecutionPolicy Bypass -File .\install.ps1 -Status
  Uninstall : powershell -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall

DSH profile config (cordis.patch.yml) has two kinds of entries:
  * {id, name, config}  -> overrides an EXISTING entry (skipped if id not found)
  * {insert: [ ... ]}   -> APPENDS new entries  <-- required for a new plugin
Relative plugin paths are anchored beside cordis.patch.yml, so
"./node_modules/aistatus-led-dsh/index.mjs" resolves to this profile's copy.

Verified with: dsh --profile web --patch <this file> --dump-config

All messages are ASCII (Windows PowerShell 5.1 reads .ps1 as ANSI).
#>
param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Status,
    [string]$ProfileDir = (Join-Path $env:USERPROFILE '.dsh\profiles\desktop')
)

$ErrorActionPreference = 'Stop'
$Src = Split-Path -Parent $PSCommandPath
$Dest = Join-Path $ProfileDir 'node_modules\aistatus-led-dsh'
$Pkg = Join-Path $ProfileDir 'package.json'
$Patch = Join-Path $ProfileDir 'cordis.patch.yml'
$Bundle = 'aistatus-led-dsh'
$BeginMark = '# >>> aistatus-led >>>'
$EndMark = '# <<< aistatus-led <<<'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$InsertBlock = @"
$BeginMark
# New plugins require an insert patch; a plain {id,name} entry is an
# id-targeted override and would be skipped. Relative path is anchored
# beside this file (the profile directory).
- insert:
    - id: aistatus-led
      name: "./node_modules/aistatus-led-dsh/index.mjs"
      config:
        port: 8765
$EndMark
"@

function Remove-PatchBlock {
    if (-not (Test-Path $Patch)) { return }
    $lines = [IO.File]::ReadAllLines($Patch)
    $out = New-Object System.Collections.Generic.List[string]
    $skip = $false
    $changed = $false
    foreach ($line in $lines) {
        if ($line.Trim() -eq $BeginMark) { $skip = $true; $changed = $true; continue }
        if ($line.Trim() -eq $EndMark) { $skip = $false; continue }
        if (-not $skip) { $out.Add($line) }
    }
    if ($changed) {
        [IO.File]::WriteAllLines($Patch, $out, $Utf8NoBom)
        Write-Host 'Removed aistatus insert block from cordis.patch.yml'
    }
}

function Remove-Bundle {
    if (-not (Test-Path $Pkg)) { return }
    $json = [IO.File]::ReadAllText($Pkg) | ConvertFrom-Json
    $b = @($json.dsh.profile.bundles)
    if ($b -contains $Bundle) {
        $json.dsh.profile.bundles = @($b | Where-Object { $_ -ne $Bundle })
        [IO.File]::WriteAllText($Pkg, ($json | ConvertTo-Json -Depth 20), $Utf8NoBom)
        Write-Host 'Removed legacy bundle entry from package.json'
    }
}

if ($Status) {
    Write-Host ('profile     : ' + $ProfileDir)
    Write-Host ('plugin_dir  : ' + $Dest + '  exists=' + (Test-Path $Dest))
    Write-Host ('insert_patch: ' + ([IO.File]::ReadAllText($Patch).Contains($BeginMark)))
    $j = [IO.File]::ReadAllText($Pkg) | ConvertFrom-Json
    Write-Host ('legacy_bundle_in_pkg: ' + (@($j.dsh.profile.bundles) -contains $Bundle))
    exit 0
}

if (-not (Test-Path $Patch)) {
    Write-Host ('ERROR: DSH profile not found: ' + $Patch)
    exit 1
}

if ($Uninstall) {
    Remove-PatchBlock
    Remove-Bundle
    if (Test-Path $Dest) {
        Remove-Item $Dest -Recurse -Force
        Write-Host ('Removed ' + $Dest)
    }
    Write-Host 'Uninstalled. Restart DeepSeek Harness to apply.'
    exit 0
}

# default action = install
$stamp = Get-Date -Format 'yyyyMMddHHmmss'
Copy-Item $Patch ($Patch + '.bak-aistatus-' + $stamp)
Write-Host ('Backup: ' + $Patch + '.bak-aistatus-' + $stamp)

Remove-PatchBlock
Remove-Bundle

New-Item -ItemType Directory -Path $Dest -Force | Out-Null
foreach ($f in @('index.mjs', 'package.json', 'cordis.patch.yml', 'README.md')) {
    Copy-Item (Join-Path $Src $f) (Join-Path $Dest $f) -Force
}
Write-Host ('Copied plugin to: ' + $Dest)

[IO.File]::AppendAllText($Patch, "`r`n" + $InsertBlock + "`r`n", $Utf8NoBom)
Write-Host 'Appended insert patch to cordis.patch.yml'
Write-Host ''
Write-Host 'DONE. Fully quit and restart DeepSeek Harness.'
Write-Host 'Verify: /events should contain aistatus/plugin-loaded.'
Write-Host 'Debug : set AISTATUS_DEBUG=1 to write ~/.dsh/aistatus-led.log'
exit 0
