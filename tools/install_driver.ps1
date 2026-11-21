# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

<#
.SYNOPSIS
  Install, uninstall or inspect the CyberFinger SteamVR driver from a local build.

.DESCRIPTION
  Install copies the built driver folder to %LOCALAPPDATA%\CyberFinger\SteamVR\cyberfinger and registers it
  with SteamVR (vrpathreg adddriver), after removing any other "cyberfinger" registration. With -InPlace it
  registers the build folder itself instead: rebuild and restart SteamVR to pick up changes (SteamVR must be
  closed while building, as it holds the DLL).

  Install and uninstall also remove what the proof-of-concept driver's README had users set up by hand: a copy
  inside SteamVR\drivers\cyberfinger, and TrackingOverrides for "/devices/cyberfinger/..." in steamvr.vrsettings
  (a backup of the file is kept as steamvr.vrsettings.cyberfinger-backup).

  Uninstall removes the registration and the copied folder. The driver's settings in steamvr.vrsettings are
  kept. Status only reports.

  SteamVR must not be running (-Force overrides; restart SteamVR afterwards).

.EXAMPLE
  tools\install_driver.cmd
  tools\install_driver.cmd -InPlace
  tools\install_driver.cmd -Status
  tools\uninstall_driver.cmd
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$Status,
    [switch]$InPlace,
    [string]$Source,   # default below: Windows PowerShell leaves $PSScriptRoot empty in param defaults under -File
    [switch]$Force
)

$ErrorActionPreference = "Stop"
if (-not $Source) { $Source = Join-Path $PSScriptRoot "..\out\build\x64-Release\driver\cyberfinger" }

# Started by double-clicking the .cmd: keep the window open so the result can be read.
function Test-StartedFromExplorer {
    try {
        $self = Get-CimInstance Win32_Process -Filter "ProcessId=$PID"
        $cmd = Get-CimInstance Win32_Process -Filter "ProcessId=$($self.ParentProcessId)"
        $outer = Get-CimInstance Win32_Process -Filter "ProcessId=$($cmd.ParentProcessId)"
        return ($cmd.Name -eq "cmd.exe") -and ($outer.Name -eq "explorer.exe")
    } catch { return $false }
}

function Exit-Script([int]$Code) {
    if (Test-StartedFromExplorer) { Read-Host "Press Enter to close" | Out-Null }
    exit $Code
}

trap {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    Exit-Script 1
}
$DriverName = "cyberfinger"
$InstallRoot = Join-Path $env:LOCALAPPDATA "CyberFinger\SteamVR"
$InstallDir = Join-Path $InstallRoot $DriverName
$VRPathFile = Join-Path $env:LOCALAPPDATA "openvr\openvrpaths.vrpath"

function Get-OpenVRPaths {
    if (-not (Test-Path $VRPathFile)) { return $null }
    return (Get-Content $VRPathFile -Raw | ConvertFrom-Json)
}

function Find-SteamVR {
    $paths = Get-OpenVRPaths
    if ($paths) {
        foreach ($rt in @($paths.runtime)) {
            if ($rt -and (Test-Path (Join-Path $rt "bin\win64\vrpathreg.exe"))) { return $rt }
        }
    }
    $steam = (Get-ItemProperty -Path "HKCU:\Software\Valve\Steam" -Name SteamPath -ErrorAction SilentlyContinue).SteamPath
    if ($steam) {
        $rt = Join-Path ($steam -replace '/', '\') "steamapps\common\SteamVR"
        if (Test-Path (Join-Path $rt "bin\win64\vrpathreg.exe")) { return $rt }
    }
    throw "SteamVR not found. Install SteamVR from Steam and start it once."
}

# The user's steamvr.vrsettings, or $null.
function Find-SteamVRSettings {
    $paths = Get-OpenVRPaths
    if ($paths) {
        foreach ($dir in @($paths.config)) {
            if ($dir -and (Test-Path (Join-Path $dir "steamvr.vrsettings"))) { return (Join-Path $dir "steamvr.vrsettings") }
        }
    }
    $steam = (Get-ItemProperty -Path "HKCU:\Software\Valve\Steam" -Name SteamPath -ErrorAction SilentlyContinue).SteamPath
    if ($steam) {
        $file = Join-Path ($steam -replace '/', '\') "config\steamvr.vrsettings"
        if (Test-Path $file) { return $file }
    }
    return $null
}

# Registered external drivers whose manifest (or folder) is named "cyberfinger".
function Get-RegisteredCyberFinger {
    $paths = Get-OpenVRPaths
    $found = @()
    if (-not $paths) { return $found }
    foreach ($p in @($paths.external_drivers)) {
        if (-not $p) { continue }
        $name = Split-Path $p -Leaf
        $manifest = Join-Path $p "driver.vrdrivermanifest"
        if (Test-Path $manifest) {
            try { $name = (Get-Content $manifest -Raw | ConvertFrom-Json).name } catch { }
        }
        if ($name -eq $DriverName) { $found += $p }
    }
    return $found
}

function Test-SteamVRRunning { return [bool](Get-Process -Name vrserver -ErrorAction SilentlyContinue) }

function Assert-SteamVRClosed {
    if (Test-SteamVRRunning) {
        if ($Force) { Write-Warning "SteamVR is running: restart it for the change to take effect."; return }
        throw "SteamVR is running. Quit SteamVR first (or pass -Force and restart SteamVR afterwards)."
    }
}

function Invoke-VRPathReg([string]$Runtime, [string[]]$Arguments) {
    & (Join-Path $Runtime "bin\win64\vrpathreg.exe") @Arguments | Out-Null
}

# TrackingOverrides for "/devices/cyberfinger/..." in a steamvr.vrsettings. They still match this driver's devices:
# whichever device holds a hand role gets the CyberFinger pose, so the hands vanish whenever the driver is off.
# With -Remove, deletes those lines (keeping a backup); everything else in the file stays byte for byte.
# Unhandled counts entries in a layout this does not edit (e.g. the whole section on one line).
function Update-OldTrackingOverrides([string]$File, [switch]$Remove) {
    $result = [pscustomobject]@{ File = $File; Entries = @(); Unhandled = 0; Removed = $false }
    if (-not $File -or -not (Test-Path -LiteralPath $File)) { return $result }
    $bytes = [System.IO.File]::ReadAllBytes($File)
    $utf8 = New-Object System.Text.UTF8Encoding($false, $true)   # no BOM added, invalid bytes throw
    try { $text = $utf8.GetString($bytes) } catch { $text = $null }
    if ($null -eq $text) {
        if ([System.Text.Encoding]::ASCII.GetString($bytes).Contains('"/devices/cyberfinger/')) { $result.Unhandled = 1 }
        return $result
    }
    if (-not $text.Contains('"/devices/cyberfinger/')) { return $result }
    $out = New-Object System.Text.StringBuilder
    $inSection = $false
    $justRemoved = $false
    foreach ($line in [regex]::Split($text, '(?<=\n)')) {
        $t = $line.Trim()
        if ($t.Contains('"TrackingOverrides"')) {
            $inSection = -not $t.Contains('}')
            if ($t.Contains('"/devices/cyberfinger/')) { $result.Unhandled++ }
        } elseif ($inSection -and $t -cmatch '^"(/devices/cyberfinger/[^"]*)"\s*:\s*"([^"]*)"\s*,?$') {
            $result.Entries += "$($Matches[1]) -> $($Matches[2])"
            $justRemoved = $true
            continue
        } elseif ($inSection -and $t.Contains('"/devices/cyberfinger/')) {
            $result.Unhandled++
        }
        if ($t) {
            if ($justRemoved -and $t.StartsWith('}')) {
                # The section's last entry went: drop the comma now left before the closing brace.
                $m = [regex]::Match($out.ToString(), ',[ \t\r\n]*\z')
                if ($m.Success) { [void]$out.Remove($m.Index, 1) }
            }
            $justRemoved = $false
            if ($inSection -and $t.StartsWith('}')) { $inSection = $false }
        }
        [void]$out.Append($line)
    }
    if ($Remove -and $result.Entries.Count -gt 0) {
        Copy-Item -LiteralPath $File -Destination "$File.cyberfinger-backup" -Force
        [System.IO.File]::WriteAllBytes($File, $utf8.GetBytes($out.ToString()))
        $result.Removed = $true
    }
    return $result
}

# Leftovers of the proof-of-concept driver, set up by hand following its README.
function Remove-OldDriverLeftovers {
    if (Test-Path $builtIn) {
        try {
            Remove-Item -LiteralPath $builtIn -Recurse -Force
            Write-Host "Removed the old copy inside SteamVR: $builtIn"
        } catch {
            Write-Warning "Could not remove $builtIn ($($_.Exception.Message)). Delete that folder so SteamVR does not load two CyberFinger drivers."
        }
    }
    $running = Test-SteamVRRunning   # SteamVR would write its own copy of the settings back
    $ovr = Update-OldTrackingOverrides (Find-SteamVRSettings) -Remove:(-not $running)
    if ($ovr.Removed) {
        foreach ($e in $ovr.Entries) { Write-Host "Removed old TrackingOverride: $e" }
        Write-Host "  from $($ovr.File) (backup: $($ovr.File).cyberfinger-backup)"
    } elseif ($ovr.Entries.Count -gt 0) {
        Write-Warning "$($ovr.File) has old CyberFinger TrackingOverrides. Run this again with SteamVR closed to remove them."
    }
    if ($ovr.Unhandled -gt 0) {
        Write-Warning "Remove the `"/devices/cyberfinger/...`" entries from TrackingOverrides in $($ovr.File) by hand (SteamVR closed)."
    }
}

$runtime = Find-SteamVR
$builtIn = Join-Path $runtime "drivers\$DriverName"
$builtDll = Join-Path $Source "bin\win64\driver_cyberfinger.dll"

if ($Status) {
    Write-Host "SteamVR          : $runtime"
    Write-Host "SteamVR running  : $(Test-SteamVRRunning)"
    $registered = @(Get-RegisteredCyberFinger)
    if ($registered.Count -eq 0) { Write-Host "Registered driver: (none)" }
    foreach ($p in $registered) { Write-Host "Registered driver: $p" }
    if (Test-Path $builtIn) { Write-Host "Inside SteamVR   : $builtIn (old copy: install or uninstall removes it)" }
    $ovr = Update-OldTrackingOverrides (Find-SteamVRSettings)
    foreach ($e in $ovr.Entries) { Write-Host "Old override     : $e (install or uninstall removes it)" }
    if ($ovr.Unhandled -gt 0) { Write-Host "Old override     : in $($ovr.File), remove by hand" }
    if (Test-Path $builtDll) { Write-Host "Local build      : $((Resolve-Path $Source).Path)" }
    else { Write-Host "Local build      : (none - run tools\build_driver.cmd)" }
    Exit-Script 0
}

if ($Uninstall) {
    Assert-SteamVRClosed
    $registered = @(Get-RegisteredCyberFinger)
    Invoke-VRPathReg $runtime @("removedriverswithname", $DriverName)
    foreach ($p in $registered) { Write-Host "Unregistered: $p" }
    if ($registered.Count -eq 0) { Write-Host "No CyberFinger driver was registered." }
    if (Test-Path $InstallRoot) {
        Remove-Item -Recurse -Force $InstallRoot
        Write-Host "Removed: $InstallRoot"
    }
    Remove-OldDriverLeftovers
    Write-Host "Done. The driver's settings in steamvr.vrsettings are left in place."
    Exit-Script 0
}

if (-not (Test-Path $builtDll)) { throw "No driver build at $Source. Run tools\build_driver.cmd first." }
Assert-SteamVRClosed

if ($InPlace) {
    $target = (Resolve-Path $Source).Path
} else {
    $target = $InstallDir
    if (Test-Path $target) { Remove-Item -Recurse -Force $target }
    New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
    Copy-Item -Recurse -Path (Resolve-Path $Source).Path -Destination $target
}

Invoke-VRPathReg $runtime @("removedriverswithname", $DriverName)   # one registration at a time
Invoke-VRPathReg $runtime @("adddriver", $target)
if (-not (@(Get-RegisteredCyberFinger) -contains $target)) { throw "SteamVR did not register $target" }
Remove-OldDriverLeftovers

Write-Host "Installed and registered: $target"
Write-Host "Start SteamVR. To switch the driver off: SteamVR Settings > Startup/Shutdown > Manage Add-ons."
Exit-Script 0
