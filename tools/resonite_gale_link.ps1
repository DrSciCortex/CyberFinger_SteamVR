# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

<#
.SYNOPSIS
Makes a Resonite build load a Gale profile's mods however it is started (Steam, SteamVR's library, a shortcut), so
neither Gale's Launch button nor a launch script is needed. Works for the Steam copy of Resonite and for other
builds, such as a private universe. Without Gale, -Standalone installs SteamVRRoleFix on its own.

.DESCRIPTION
Gale starts Resonite with arguments that tell BepInEx where the profile is. Linking puts that into the build's
folder instead (see the README, "Loading Gale mods without Gale's launcher"):

  <build>\hookfxr.ini                     enable=true: the engine-side loader (BepisLoader) runs on every start
  <build>\hostfxr.dll, BepisLoader.*      that loader, copied from the profile (when the build lacks it)
  <build>\BepInEx                         a junction to <profile>\BepInEx (the engine-side plugins)
  <build>\Renderer\doorstop_config.ini    enabled=true: the renderer-side loader (doorstop) runs on every start
  <build>\Renderer\winhttp.dll            that loader, from the profile's RenderiteHook (when the build lacks it)
  <build>\Renderer\BepInEx                a junction to <profile>\Renderer\BepInEx (the renderer-side plugins)
  <profile>\hookfxr.ini                   enable=true as well: Gale copies it into the build when it launches

Files and folders the build had are kept as *.pre-gale-link. <build>\CyberFinger-GaleLink.json records every
change, and -Unlink undoes them. -Mods Off starts the build without any mods, -Engine Off without the
engine-side ones; -Mods On / -Engine On switch them back.

.PARAMETER Game
The build's folder, or its launcher (Resonite.exe, or another build's, ...). Without it, the builds found are listed.

.PARAMETER GaleProfile
The Gale profile, by name or folder. Default: the profile named like the build, for the Steam copy
"Resonite" or else "Default".

.EXAMPLE
resonite_gale_link.cmd
Lists the builds found, and asks which to link to which profile.

.EXAMPLE
resonite_gale_link.cmd -Status

.EXAMPLE
resonite_gale_link.cmd -Game "C:\Games\YourPrivateUniverse-1.2.3.456" -GaleProfile YourPrivateUniverse

.EXAMPLE
resonite_gale_link.cmd -Game "C:\Games\YourPrivateUniverse-1.2.3.456" -Mods Off
Starts it without mods from now on (troubleshooting); -Mods On loads them again.

.EXAMPLE
resonite_gale_link.cmd -Game "C:\Games\YourPrivateUniverse-1.2.3.456" -Unlink

.EXAMPLE
resonite_gale_link.cmd -Unlink -All
Undoes every link made with this script on this PC (CyberFinger Bridge's uninstaller runs this).
#>
[CmdletBinding()]
param(
    [string]$Game,
    [Alias('Profile')][string]$GaleProfile,
    [switch]$Standalone,
    [string]$Source,
    [switch]$Unlink,
    [switch]$All,
    [ValidateSet('', 'On', 'Off')][string]$Engine = '',
    [ValidateSet('', 'On', 'Off')][string]$Mods = '',
    [switch]$Status,
    [switch]$Plan,
    [switch]$Yes,
    [switch]$Force,
    [string]$GaleData,
    [string]$Out
)

$ErrorActionPreference = 'Stop'
$RecordName = 'CyberFinger-GaleLink.json'
$MarkerName = 'CyberFinger-GaleLink.txt'      # in a linked profile: the builds that load it, for whoever looks there
$IndexPath = Join-Path $env:LOCALAPPDATA 'CyberFinger\gale_links.json'
$EngineFiles = @('hostfxr.dll', 'BepisLoader.dll', 'BepisLoader.deps.json', 'BepisLoader.runtimeconfig.json')
$PluginOnlyStandalone = 'DrSciCortex-SteamVRRoleFix'
$script:Errors = 0
$script:Interactive = -not $Yes -and -not $Plan -and [Environment]::UserInteractive
$script:Utf8 = New-Object System.Text.UTF8Encoding($false)

# -- output ---------------------------------------------------------------------

function Write-Out([string]$Text) {
    if (-not $Out -or $script:OutFailed) { return }
    try {
        $dir = Split-Path -Parent $Out
        if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        [System.IO.File]::AppendAllText($Out, $Text + "`r`n", (New-Object System.Text.UTF8Encoding($true)))
    } catch {
        # The report only copies what is shown: losing it must not stop a change halfway.
        $script:OutFailed = $true
        Write-Host "  WARN   The report could not be written to ${Out}: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

function Say([string]$Kind, [string]$Text) {
    $prefix = @{ ok = '  OK     '; warn = '  WARN   '; err = '  ERROR  '; next = '  NEXT   '; info = '         '; head = '' }[$Kind]
    $color = @{ ok = 'Green'; warn = 'Yellow'; err = 'Red'; next = 'Cyan'; info = 'Gray'; head = 'White' }[$Kind]
    if ($Kind -eq 'err') { $script:Errors++ }
    if ($Plan) { return }
    Write-Host ($prefix + $Text) -ForegroundColor $color
    Write-Out ($prefix + $Text)
}

function Confirm-Step([string]$Question) {
    if (-not $script:Interactive) { return $true }
    $a = Read-Host "$Question [Y/n]"
    return ($a -eq '' -or $a -match '^(y|yes)$')
}

# -- Steam, shortcuts and builds -----------------------------------------------

function Get-SteamPath {
    try { $p = (Get-ItemProperty -Path 'HKCU:\Software\Valve\Steam' -Name SteamPath -ErrorAction Stop).SteamPath } catch { $p = $null }
    if ($p) { return ($p -replace '/', '\') }
    $d = Join-Path ${env:ProgramFiles(x86)} 'Steam'
    if (Test-Path -LiteralPath $d) { return $d }
    return $null
}

# The Steam copy of Resonite: in one of Steam's libraries.
function Get-SteamResonite {
    $steam = Get-SteamPath
    if (-not $steam) { return $null }
    $libs = @($steam)
    $vdf = Join-Path $steam 'steamapps\libraryfolders.vdf'
    if (Test-Path -LiteralPath $vdf) {
        foreach ($m in [regex]::Matches((Get-Content -LiteralPath $vdf -Raw), '"path"\s+"([^"]+)"')) { $libs += ($m.Groups[1].Value -replace '\\\\', '\') }
    }
    foreach ($l in $libs | Select-Object -Unique) {
        $d = Join-Path $l 'steamapps\common\Resonite'
        if (Test-ResoniteBuild $d) { return (Get-FullDir $d) }
    }
    return $null
}

# Steam's binary KeyValues (shortcuts.vdf): 0 = map, 1 = string, 2 = int32, 8 = end of map. Keys are lowercased.
function Read-BinaryVdf([byte[]]$Bytes) { $script:vdf = $Bytes; $script:vdfPos = 0; return (Read-VdfMap) }
function Read-VdfString {
    $end = [Array]::IndexOf($script:vdf, [byte]0, $script:vdfPos)
    if ($end -lt 0) { throw 'unterminated string' }
    $s = [System.Text.Encoding]::UTF8.GetString($script:vdf, $script:vdfPos, $end - $script:vdfPos)
    $script:vdfPos = $end + 1
    return $s
}
function Read-VdfMap {
    $m = @{}
    while ($script:vdfPos -lt $script:vdf.Length) {
        $t = $script:vdf[$script:vdfPos]; $script:vdfPos++
        if ($t -eq 8) { return $m }
        $k = (Read-VdfString).ToLowerInvariant()
        switch ($t) {
            0 { $m[$k] = Read-VdfMap }
            1 { $m[$k] = Read-VdfString }
            2 { $m[$k] = [BitConverter]::ToUInt32($script:vdf, $script:vdfPos); $script:vdfPos += 4 }
            default { throw "unknown entry type $t" }
        }
    }
    return $m
}

function New-Shortcut([string]$Name, [string]$Exe, [string]$Arguments, [string]$Kind, [string]$Where) {
    return [pscustomobject]@{ Name = $Name; Exe = $Exe; Args = $Arguments; Kind = $Kind; Where = $Where }
}

# What starts things: Steam's non-Steam games, then desktop and Start menu shortcuts.
function Get-Shortcuts {
    $list = New-Object System.Collections.Generic.List[object]
    $steam = Get-SteamPath
    if ($steam) {
        foreach ($f in @(Get-ChildItem -Path (Join-Path $steam 'userdata\*\config\shortcuts.vdf') -ErrorAction SilentlyContinue)) {
            try { $root = Read-BinaryVdf ([System.IO.File]::ReadAllBytes($f.FullName)) } catch { continue }
            if (-not $root['shortcuts']) { continue }
            foreach ($s in $root['shortcuts'].Values) {
                $exe = ([string]$s['exe']).Trim().Trim('"')
                if ($exe) { $list.Add((New-Shortcut ([string]$s['appname']) $exe ([string]$s['launchoptions']) 'steam' "Steam shortcut '$($s['appname'])'")) }
            }
        }
    }
    try { $shell = New-Object -ComObject WScript.Shell } catch { $shell = $null }
    if ($shell) {
        foreach ($d in @('Desktop', 'CommonDesktopDirectory', 'Programs', 'CommonPrograms')) {
            $dir = [Environment]::GetFolderPath($d)
            if (-not $dir -or -not (Test-Path -LiteralPath $dir)) { continue }
            foreach ($l in @(Get-ChildItem -LiteralPath $dir -Recurse -Filter '*.lnk' -ErrorAction SilentlyContinue)) {
                try { $sc = $shell.CreateShortcut($l.FullName) } catch { continue }
                if ($sc.TargetPath -like '*.exe') { $list.Add((New-Shortcut $l.BaseName $sc.TargetPath $sc.Arguments 'lnk' "shortcut $($l.FullName)")) }
            }
        }
    }
    return $list
}

function Test-ResoniteBuild([string]$Dir) {
    if (-not $Dir -or -not (Test-Path -LiteralPath $Dir -PathType Container)) { return $false }
    $engine = (Test-Path -LiteralPath (Join-Path $Dir 'Renderite.Host.exe')) -or (Test-Path -LiteralPath (Join-Path $Dir 'Renderite.Host.dll'))
    return $engine -and (Test-Path -LiteralPath (Join-Path $Dir 'Renderer\Renderite.Renderer.exe'))
}

function Get-FullDir([string]$Path) {
    $p = [System.IO.Path]::GetFullPath($Path.Trim().Trim('"'))
    if (Test-Path -LiteralPath $p -PathType Leaf) { $p = Split-Path -Parent $p }
    $p = $p.TrimEnd('\')
    # The names as on disk (Steam's registry entry is lowercase, for one)
    $parts = $p.Split('\')
    $real = $parts[0].ToUpperInvariant()
    for ($i = 1; $i -lt $parts.Length; $i++) {
        $hit = @(Get-ChildItem -LiteralPath ($real + '\') -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq $parts[$i] })
        $real += '\' + $(if ($hit.Count) { $hit[0].Name } else { $parts[$i] })
    }
    return $real
}

function Get-Index {
    if (-not (Test-Path -LiteralPath $IndexPath)) { return @() }
    try { $j = Get-Content -LiteralPath $IndexPath -Raw | ConvertFrom-Json } catch { return @() }
    if ($j -and $j.PSObject.Properties['value']) { $j = $j.value }   # as Windows PowerShell 5.1 once wrote it
    return @($j | Where-Object { $_ -is [string] -and $_ })
}
function Set-Index([object[]]$Paths) {
    $dir = Split-Path -Parent $IndexPath
    $list = [string[]]@($Paths | Where-Object { $_ } | ForEach-Object { [string]$_ } | Sort-Object -Unique)
    if (-not $list.Count) {   # nothing linked any more: no trace left
        if (Test-Path -LiteralPath $IndexPath) { Remove-Item -LiteralPath $IndexPath -Force }
        if ((Test-Path -LiteralPath $dir) -and -not @(Get-ChildItem -LiteralPath $dir -Force).Count) { Remove-Item -LiteralPath $dir -Force }
        return
    }
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    # A plain JSON list: ConvertTo-Json in Windows PowerShell 5.1 wraps some arrays as {"value": [...], "Count": n}
    $json = '[' + (($list | ForEach-Object { ConvertTo-Json -InputObject $_ }) -join ', ') + ']'
    [System.IO.File]::WriteAllText($IndexPath, $json, $script:Utf8)
}

# Builds: the Steam copy, the -Game one, earlier links, and those shortcuts start.
function Get-Builds([object[]]$Shortcuts) {
    $steamCopy = Get-SteamResonite
    $byPath = [ordered]@{}
    $dirs = @()
    if ($steamCopy) { $dirs += $steamCopy }
    if ($Game) { $dirs += $Game }
    $dirs += @(Get-Index)
    $dirs += @($Shortcuts | ForEach-Object { Split-Path -Parent $_.Exe })
    foreach ($d in $dirs) {
        if (-not (Test-ResoniteBuild $d)) { continue }
        $full = Get-FullDir $d
        if ($byPath.Contains($full)) { continue }
        $byPath[$full] = [pscustomobject]@{ Path = $full; Name = $null; Steam = ($full -eq $steamCopy)
                                           Shortcuts = New-Object System.Collections.Generic.List[object] }
    }
    try { $shell = New-Object -ComObject WScript.Shell } catch { $shell = $null }
    foreach ($b in $byPath.Values) {
        foreach ($s in $Shortcuts) { if ((Get-FullDir (Split-Path -Parent $s.Exe)) -eq $b.Path) { $b.Shortcuts.Add($s) } }
        if ($shell) {   # shortcuts kept in the build's own folder, e.g. "YourPrivateUniverse Launcher.lnk"
            foreach ($l in @(Get-ChildItem -LiteralPath $b.Path -Filter '*.lnk' -ErrorAction SilentlyContinue)) {
                $sc = $shell.CreateShortcut($l.FullName)
                if ($sc.TargetPath -and (Get-FullDir (Split-Path -Parent $sc.TargetPath)) -eq $b.Path) {
                    $b.Shortcuts.Add((New-Shortcut $l.BaseName $sc.TargetPath $sc.Arguments 'lnk' "shortcut $($l.FullName)"))
                }
            }
        }
        if ($b.Steam) { $b.Name = 'Resonite (Steam)'; continue }
        $named = @($b.Shortcuts | Where-Object { $_.Kind -eq 'steam' -and $_.Name } | Select-Object -First 1)
        $launcher = @(Get-ChildItem -LiteralPath $b.Path -Filter '*.exe' -ErrorAction SilentlyContinue |
                      Where-Object { $_.Name -notmatch '^Renderite\.' } | Select-Object -First 1)
        if ($named.Count) { $b.Name = $named[0].Name }
        elseif ($launcher.Count) { $b.Name = $launcher[0].BaseName }
        else { $b.Name = (Split-Path -Leaf $b.Path) -replace '-\d+(\.\d+)+$', '' }
    }
    return @($byPath.Values)
}

# -- Gale -----------------------------------------------------------------------

function Get-GaleDir {
    if ($GaleData) { return $GaleData.TrimEnd('\') }
    $d = Join-Path $env:APPDATA 'com.kesomannen.gale'
    # Gale can move its data folder (its settings) and records where in its database.
    $db = Join-Path $d 'data.sqlite3'
    if (Test-Path -LiteralPath $db) {
        try {
            $fs = [System.IO.File]::Open($db, 'Open', 'Read', 'ReadWrite')
            try { $buf = New-Object byte[] $fs.Length; [void]$fs.Read($buf, 0, $buf.Length) } finally { $fs.Close() }
            $m = [regex]::Match([System.Text.Encoding]::UTF8.GetString($buf), '"dataDir"\s*:\s*"((?:[^"\\]|\\.)+)"')
            if ($m.Success) {
                $alt = $m.Groups[1].Value -replace '\\\\', '\'
                if (Test-Path -LiteralPath (Join-Path $alt 'resonite\profiles')) { return $alt.TrimEnd('\') }
            }
        } catch { }
    }
    return $d
}

function Get-GaleProfiles([string]$GaleDir) {
    $root = Join-Path $GaleDir 'resonite\profiles'
    if (-not (Test-Path -LiteralPath $root)) { return @() }
    return @(Get-ChildItem -LiteralPath $root -Directory | ForEach-Object { $_.FullName })
}

# A plugin folder of the profile; one disabled in Gale (its files renamed *.old) doesn't count.
function Find-Plugin([string]$Plugins, [string]$Name) {
    if (-not (Test-Path -LiteralPath $Plugins)) { return $null }
    return @(Get-ChildItem -LiteralPath $Plugins -Directory -ErrorAction SilentlyContinue |
             Where-Object { $_.Name -like "*$Name*" -and $_.Name -notlike '*.old' -and
                            -not (Test-Path -LiteralPath (Join-Path $_.FullName 'manifest.json.old')) } |
             ForEach-Object { $_.FullName })[0]
}

# What a profile supplies: the engine side (BepisLoader), the renderer side (BepInExRenderer), and the renderer's
# loader (RenderiteHook carries it; Gale's download cache as a fallback).
function Get-ProfileInfo([string]$ProfilePath, [string]$GaleDir) {
    $missing = @($EngineFiles + 'hookfxr.ini' | Where-Object { -not (Test-Path -LiteralPath (Join-Path $ProfilePath $_)) })
    $engineOk = (Test-Path -LiteralPath (Join-Path $ProfilePath 'BepInEx\core')) -and -not $missing.Count
    $rendererOk = Test-Path -LiteralPath (Join-Path $ProfilePath 'Renderer\BepInEx\core\BepInEx.Preloader.dll')
    $doorstop = $null
    foreach ($r in @((Join-Path $ProfilePath 'BepInEx\plugins'), (Join-Path $GaleDir 'cache\ResoniteModding-RenderiteHook'))) {
        if ($doorstop -or -not (Test-Path -LiteralPath $r)) { continue }
        $hit = @(Get-ChildItem -LiteralPath $r -Recurse -Filter 'winhttp.dll' -ErrorAction SilentlyContinue |
                 Where-Object { $_.Directory.Name -eq 'Doorstop' } | Sort-Object LastWriteTime -Descending | Select-Object -First 1)
        if ($hit.Count -and (Test-Path -LiteralPath (Join-Path $hit[0].DirectoryName 'doorstop_config.ini'))) { $doorstop = $hit[0].DirectoryName }
    }
    return [pscustomobject]@{
        Path = $ProfilePath; Name = Split-Path -Leaf $ProfilePath
        Engine = [bool]$engineOk; Renderer = [bool]$rendererOk; Doorstop = $doorstop
        Rmll = Find-Plugin (Join-Path $ProfilePath 'BepInEx\plugins') 'ResoniteModLoaderLoader'
    }
}

function Find-ProfileFor($Build, [string]$GaleDir) {
    $profiles = Get-GaleProfiles $GaleDir
    if ($GaleProfile) {
        if (Test-Path -LiteralPath $GaleProfile -PathType Container) { return (Get-FullDir $GaleProfile) }
        return @($profiles | Where-Object { (Split-Path -Leaf $_) -eq $GaleProfile })[0]
    }
    $rec = Get-Record $Build.Path
    if ($rec -and $rec.mode -eq 'gale' -and $rec.profile -and (Test-Path -LiteralPath $rec.profile)) { return $rec.profile }
    $names = if ($Build.Steam) { @('Resonite', 'Default') } else { @($Build.Name, ((Split-Path -Leaf $Build.Path) -replace '-\d+(\.\d+)+$', '')) }
    foreach ($n in $names) {
        $hit = @($profiles | Where-Object { (Split-Path -Leaf $_) -eq $n })
        if ($hit.Count) { return $hit[0] }
    }
    return $null
}

# -- changes to the build, recorded so that -Unlink can undo them -------------

function Get-Record([string]$BuildPath) {
    $f = Join-Path $BuildPath $RecordName
    if (-not (Test-Path -LiteralPath $f)) { return $null }
    try { return (Get-Content -LiteralPath $f -Raw | ConvertFrom-Json) } catch { return $null }
}

function New-Record([string]$Mode, $Old) {
    $rec = [pscustomobject]@{ version = 1; mode = $Mode; profile = ''; engine = $false; renderer = $false
                              files = @(); dirs = @(); junctions = @(); backups = @(); iniEdits = @(); updated = '' }
    if ($Old) {   # changes made earlier stay recorded
        foreach ($k in 'files', 'dirs', 'junctions', 'backups', 'iniEdits') { if ($Old.$k) { $rec.$k = @($Old.$k) } }
    }
    return $rec
}

function Save-Record([string]$BuildPath, $Rec) {
    $Rec.updated = (Get-Date).ToString('s')
    [System.IO.File]::WriteAllText((Join-Path $BuildPath $RecordName), ($Rec | ConvertTo-Json -Depth 5), $script:Utf8)
}

function Get-Running([string]$Dir) {
    $prefix = $Dir.TrimEnd('\') + '\'
    return @(Get-Process -ErrorAction SilentlyContinue |
             Where-Object { $_.Path -and $_.Path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) } |
             ForEach-Object { $_.ProcessName } | Sort-Object -Unique)
}

function Get-JunctionTarget([string]$Path) {
    $i = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($i -and $i.LinkType -eq 'Junction') { return [string](@($i.Target)[0]) }
    return $null
}
function Remove-Junction([string]$Path) {
    if (Get-JunctionTarget $Path) { [System.IO.Directory]::Delete($Path, $false) }   # the link only, never the target
}

function Test-RuntimeHostfxr([string]$File) {
    return ((Test-Path -LiteralPath $File) -and ((Get-Item -LiteralPath $File).VersionInfo.CompanyName -match 'Microsoft'))
}

function Move-Aside([string]$Path, $Rec) {
    $aside = "$Path.pre-gale-link"
    if (Test-Path -LiteralPath $aside) { $aside = "$Path.pre-gale-link-$(Get-Date -Format yyyyMMddHHmmss)" }
    Rename-Item -LiteralPath $Path -NewName (Split-Path -Leaf $aside)
    $Rec.backups += [pscustomobject]@{ path = $Path; backup = $aside }
    return $aside
}

# Points <Link> at <Target>. A real folder in the way, holding no plugins (BepInEx's own core and config only), is
# moved aside; one with plugins only with -Force, as they would stop loading.
function Set-Junction([string]$Link, [string]$Target, $Rec) {
    $current = Get-JunctionTarget $Link
    if ($current) {
        if ($current.TrimEnd('\') -eq $Target.TrimEnd('\')) {
            if (-not ($Rec.junctions -contains $Link)) { $Rec.junctions += $Link }
            return $true
        }
        Remove-Junction $Link
    } elseif (Test-Path -LiteralPath $Link) {
        $plugins = @(Get-ChildItem -LiteralPath (Join-Path $Link 'plugins') -Recurse -File -Force -ErrorAction SilentlyContinue)
        if ($plugins.Count -and -not $Force) {
            Say err "$Link holds $($plugins.Count) plugin file(s) of its own; linking would stop them loading."
            Say next 'Move those plugins into the Gale profile (or remove them), then run this again; or run again with -Force to move the folder aside.'
            return $false
        }
        $aside = Move-Aside $Link $Rec
        Say info "Moved the build's own $(Split-Path -Leaf $Link) folder aside: $aside"
    }
    New-Item -ItemType Junction -Path $Link -Target $Target | Out-Null
    if (-not ($Rec.junctions -contains $Link)) { $Rec.junctions += $Link }
    return $true
}

# Copies a file in. A file the build had that differs is kept as *.pre-gale-link; an identical one is left alone.
function Copy-In([string]$SourceFile, [string]$Dest, $Rec) {
    if ((Test-Path -LiteralPath $Dest) -and -not ($Rec.files -contains $Dest)) {
        if ((Get-FileHash -LiteralPath $SourceFile).Hash -eq (Get-FileHash -LiteralPath $Dest).Hash) { return }
        $bak = "$Dest.pre-gale-link"
        if (-not (Test-Path -LiteralPath $bak)) { Copy-Item -LiteralPath $Dest -Destination $bak -Force; $Rec.backups += [pscustomobject]@{ path = $Dest; backup = $bak } }
    }
    Copy-Item -LiteralPath $SourceFile -Destination $Dest -Force
    if (-not ($Rec.files -contains $Dest)) { $Rec.files += $Dest }
}

# An ini file with some keys set: the source's lines, else the file's own.
function Write-Ini([string]$SourceFile, [string]$Dest, [hashtable]$Set, $Rec) {
    $lines = @(Get-Content -LiteralPath $SourceFile | ForEach-Object {
        $line = $_
        foreach ($k in $Set.Keys) { if ($line -match "^\s*$([regex]::Escape($k))\s*=") { $line = "$k=$($Set[$k])" } }
        $line
    })
    $tmp = "$Dest.new"
    [System.IO.File]::WriteAllLines($tmp, [string[]]$lines, $script:Utf8)
    Copy-In $tmp $Dest $Rec
    Remove-Item -LiteralPath $tmp -Force
}

function Get-IniValue([string]$File, [string]$Key) {
    if (-not (Test-Path -LiteralPath $File)) { return $null }
    $m = Select-String -LiteralPath $File -Pattern "^\s*$([regex]::Escape($Key))\s*=\s*(.*)$" | Select-Object -First 1
    if ($m) { return $m.Matches[0].Groups[1].Value.Trim() }
    return $null
}

# Sets one key in place (no backup): the switches, and the profile's hookfxr.ini.
function Set-IniValue([string]$File, [string]$Key, [string]$Value) {
    $lines = @(Get-Content -LiteralPath $File | ForEach-Object { if ($_ -match "^\s*$([regex]::Escape($Key))\s*=") { "$Key=$Value" } else { $_ } })
    [System.IO.File]::WriteAllLines($File, [string[]]$lines, $script:Utf8)
}

function Update-Marker([string]$ProfilePath, [string]$BuildPath, [bool]$Add) {
    if (-not $ProfilePath -or -not (Test-Path -LiteralPath $ProfilePath)) { return }
    $f = Join-Path $ProfilePath $MarkerName
    $have = @()
    if (Test-Path -LiteralPath $f) { $have = @(Get-Content -LiteralPath $f | Where-Object { $_ -and $_ -ne $BuildPath }) }
    if ($Add) { $have += $BuildPath }
    if ($have.Count) { [System.IO.File]::WriteAllLines($f, [string[]]$have, $script:Utf8) }
    elseif (Test-Path -LiteralPath $f) { Remove-Item -LiteralPath $f -Force }
}

# -- how the build starts ResoniteModLoader, and leftovers of launch scripts ---

# The text inside the VDF section whose opening brace is at $Open (quoted strings may hold braces), or $null.
function Get-VdfSection([string]$Text, [int]$Open) {
    $depth = 0
    for ($i = $Open; $i -lt $Text.Length; $i++) {
        $c = $Text[$i]
        if ($c -eq '"') {
            for ($i++; $i -lt $Text.Length -and $Text[$i] -ne '"'; $i++) { if ($Text[$i] -eq '\') { $i++ } }
        } elseif ($c -eq '{') { $depth++ }
        elseif ($c -eq '}') { $depth--; if ($depth -eq 0) { return $Text.Substring($Open + 1, $i - $Open - 1) } }
    }
    return $null
}

# An app's Steam launch options, from its own section of localconfig.vdf: the logged-in Steam user's first, as
# Steam applies those.
function Get-SteamLaunchOptions([int]$AppId) {
    $steam = Get-SteamPath
    if (-not $steam) { return $null }
    try { $active = [string](Get-ItemProperty -Path 'HKCU:\Software\Valve\Steam\ActiveProcess' -Name ActiveUser -ErrorAction Stop).ActiveUser } catch { $active = '' }
    $files = @(Get-ChildItem -Path (Join-Path $steam 'userdata\*\config\localconfig.vdf') -ErrorAction SilentlyContinue |
               Sort-Object { if ($_.Directory.Parent.Name -eq $active) { 0 } else { 1 } })
    foreach ($f in $files) {
        $text = Get-Content -LiteralPath $f.FullName -Raw
        foreach ($m in [regex]::Matches($text, '"' + $AppId + '"\s*\{')) {
            $section = Get-VdfSection $text ($m.Index + $m.Length - 1)
            if (-not $section) { continue }
            $lo = [regex]::Match($section, '"LaunchOptions"\s+"((?:[^"\\]|\\.)*)"')
            if ($lo.Success) { return ($lo.Groups[1].Value -replace '\\"', '"' -replace '\\\\', '\') }
        }
    }
    return $null
}

# What starts the build: its shortcuts, and for the Steam copy its Steam launch options.
function Get-Starters($Build) {
    $starters = @()
    foreach ($s in $Build.Shortcuts) { $starters += $s }   # (@() around the List throws in Windows PowerShell 5.1)
    if ($Build.Steam) {
        $opts = Get-SteamLaunchOptions 2519830
        if ($opts) { $starters += (New-Shortcut 'Resonite' '' $opts 'steamapp' 'Steam launch options of Resonite') }
    }
    return $starters
}

# The starters that load ResoniteModLoader themselves (-LoadAssembly), BepInEx or not.
function Get-RmlStarters($Build) {
    return @(Get-Starters $Build | Where-Object { $_.Args -match '(?i)-LoadAssembly\s+"?\S*ResoniteModLoader' })
}

# Where to take something out of a starter.
function Get-StarterFix($Starter, [string]$What) {
    switch ($Starter.Kind) {
        'steam' { return "In Steam: right-click '$($Starter.Name)' > Properties > Launch options, and remove $What." }
        'steamapp' { return "In Steam: right-click Resonite > Properties > General > Launch options, and remove $What, keeping your own options (Gale launches through Steam, so they apply there too)." }
        default { return "Right-click that shortcut > Properties > Target, and remove $What." }
    }
}

# Checks how the build starts against the supported setup: ResoniteModLoaderLoader in the Gale profile loads
# ResoniteModLoader, and nothing that starts the build passes -LoadAssembly, a launch script or BepInEx arguments.
function Test-Launches($Build, $Info) {
    $rmlInBuild = Test-Path -LiteralPath (Join-Path $Build.Path 'Libraries\ResoniteModLoader.dll')
    $rmll = [bool]($Info -and $Info.Rmll -and $Info.Engine)
    $pname = if ($Info) { "'$($Info.Name)'" } else { '' }
    foreach ($s in @(Get-Starters $Build)) {
        if (($s.Exe -match '(?i)\.(bat|cmd)$') -or ($s.Args -match '(?i)\.(bat|cmd)\b|--bepinex-target|--hookfxr-enable|--doorstop-')) {
            Say info "$($s.Where) starts it through a launch script or with BepInEx arguments. No longer needed: it still works (arguments take precedence), but can point at another profile than the link."
            Say next (Get-StarterFix $s 'the launch script (such as LaunchModded.bat %command%) and the BepInEx arguments')
        }
    }
    $loadsRml = @(Get-RmlStarters $Build)
    foreach ($s in $loadsRml) {
        if ($rmll) {
            Say warn "$($s.Where) passes -LoadAssembly Libraries/ResoniteModLoader.dll, and the profile's ResoniteModLoaderLoader loads ResoniteModLoader too. Loaded twice, it stops Resonite as it starts (""An item with the same key has already been added"")."
        } else {
            Say info "$($s.Where) loads ResoniteModLoader itself (-LoadAssembly). It works, but the supported setup is ResoniteModLoaderLoader in the Gale profile: RML then loads with the profile however the build starts, and -Mods Off switches its mods off too."
        }
        Say next (Get-StarterFix $s '-LoadAssembly Libraries/ResoniteModLoader.dll')
    }
    if ($loadsRml.Count -and -not $rmll) { Say next "At the same time, in Gale, install or enable ResoniteModLoaderLoader in profile $pname." }
    if ($rmll -and -not $rmlInBuild) {
        Say warn "The profile's ResoniteModLoaderLoader loads $($Build.Path)\Libraries\ResoniteModLoader.dll, which isn't there."
        Say next 'Install ResoniteModLoader into the build (Libraries\ResoniteModLoader.dll, rml_libs\0Harmony.dll): https://github.com/resonite-modding-group/ResoniteModLoader'
    }
    if ($rmlInBuild -and -not $rmll -and -not $loadsRml.Count) {
        Say warn 'ResoniteModLoader is in the build, but nothing loads it: its mods (CyberFingerMod, ProximityGrab) will not load.'
        Say next "In Gale, install or enable ResoniteModLoaderLoader in profile $pname."
    }
}

# -- link to a Gale profile -----------------------------------------------------

function Invoke-Link($Build, [string]$ProfilePath, [string]$GaleDir) {
    $info = Get-ProfileInfo $ProfilePath $GaleDir
    Say head ''
    Say head "Linking $($Build.Name)  ($($Build.Path))"
    Say head "  to Gale profile '$($info.Name)'  ($ProfilePath)"
    $running = Get-Running $Build.Path
    if ($running.Count) { Say err "$($Build.Name) is running ($($running -join ', '))."; Say next 'Close it, then run this again.'; return $false }
    $old = Get-Record $Build.Path
    if ($old -and $old.mode -eq 'standalone') {
        Say info 'It had SteamVRRoleFix installed without Gale: removing that first (the Gale profile takes over).'
        if (-not (Invoke-Unlink $Build $true)) { return $false }
        $old = $null
    }
    $wantEngine = $info.Engine
    if ($wantEngine -and (Test-RuntimeHostfxr (Join-Path $Build.Path 'hostfxr.dll'))) {
        $wantEngine = $false
        Say warn 'The build carries its own .NET runtime (a hostfxr.dll by Microsoft), which BepisLoader cannot load in front of: engine-side mods will not load.'
    }
    $wantRenderer = $info.Renderer -and $info.Doorstop
    if (-not $info.Engine) { Say warn "Profile '$($info.Name)' has no BepisLoader: engine-side mods will not load. In Gale, install BepisLoader into it." }
    if (-not $info.Renderer) { Say warn "Profile '$($info.Name)' has no BepInExRenderer: renderer-side mods (SteamVRRoleFix) will not load. In Gale, install BepInExRenderer into it." }
    elseif (-not $info.Doorstop) { Say warn "Profile '$($info.Name)' has no RenderiteHook, which carries the renderer's loader: renderer-side mods will not load. In Gale, install RenderiteHook into it." }
    if (-not $wantEngine -and -not $wantRenderer) {
        Say err 'Nothing to link: the profile has neither loader.'
        Say next "In Gale, select profile '$($info.Name)' and install BepisLoader, BepInExRenderer and RenderiteHook (or copy a working profile), then run this again."
        return $false
    }
    $rec = New-Record 'gale' $old
    $rec.profile = $ProfilePath; $rec.engine = $wantEngine; $rec.renderer = $wantRenderer
    if ($old -and $old.profile -and $old.profile -ne $ProfilePath) {
        Update-Marker $old.profile $Build.Path $false
        Say info "It was linked to $($old.profile): relinking."
    }
    try {
        if ($wantEngine) {
            foreach ($f in $EngineFiles) { Copy-In (Join-Path $ProfilePath $f) (Join-Path $Build.Path $f) $rec }
            Write-Ini (Join-Path $ProfilePath 'hookfxr.ini') (Join-Path $Build.Path 'hookfxr.ini') @{ enable = 'true'; target_assembly = 'BepisLoader.dll' } $rec
            # Gale copies the profile's hookfxr.ini into the game folder when it launches: switch it on there too.
            $pini = Join-Path $ProfilePath 'hookfxr.ini'
            $was = Get-IniValue $pini 'enable'
            if ($was -ne 'true') {
                if (-not @($rec.iniEdits | Where-Object { $_.file -eq $pini })) { $rec.iniEdits += [pscustomobject]@{ file = $pini; key = 'enable'; old = $was } }
                Set-IniValue $pini 'enable' 'true'
            }
            if (-not (Set-Junction (Join-Path $Build.Path 'BepInEx') (Join-Path $ProfilePath 'BepInEx') $rec)) { Save-Record $Build.Path $rec; return $false }
            Say ok "Engine side: hookfxr.ini enable=true, BepInEx -> the profile's BepInEx (BepisLoader plugins)"
        }
        if ($wantRenderer) {
            $r = Join-Path $Build.Path 'Renderer'
            foreach ($f in @('winhttp.dll', '.doorstop_version')) {
                $src = Join-Path $info.Doorstop $f
                if (Test-Path -LiteralPath $src) { Copy-In $src (Join-Path $r $f) $rec }
            }
            Write-Ini (Join-Path $info.Doorstop 'doorstop_config.ini') (Join-Path $r 'doorstop_config.ini') @{ enabled = 'true'; target_assembly = 'BepInEx\core\BepInEx.Preloader.dll' } $rec
            if (-not (Set-Junction (Join-Path $r 'BepInEx') (Join-Path $ProfilePath 'Renderer\BepInEx') $rec)) { Save-Record $Build.Path $rec; return $false }
            Say ok "Renderer side: Renderer\doorstop_config.ini enabled=true, Renderer\BepInEx -> the profile's Renderer\BepInEx"
        }
    } catch {
        Say err "Linking failed: $($_.Exception.Message)"
        Say next "Check that nothing from $($Build.Path) is running and that you can write there, then run this again (or -Unlink to undo what was done)."
        Save-Record $Build.Path $rec
        return $false
    }
    Save-Record $Build.Path $rec
    Update-Marker $ProfilePath $Build.Path $true
    Set-Index (@(Get-Index) + $Build.Path)
    Test-Launches $Build $info
    Say ok "Linked. Every change is recorded in $($Build.Path)\$RecordName."
    Say next "Start $($Build.Name) as usual: it loads Gale profile '$($info.Name)'. Gale's Launch button isn't needed (it still works, with the profile selected in Gale)."
    Say next "Logs: $ProfilePath\BepInEx\LogOutput.log (engine side), $ProfilePath\Renderer\BepInEx\LogOutput.log (renderer side)."
    Say next "Troubleshooting: -Mods Off starts it without any mods, -Engine Off without the engine-side ones; -Unlink undoes the link:"
    Say info "  resonite_gale_link -Game ""$($Build.Path)"" -Mods Off"
    return $true
}

# -- SteamVRRoleFix without Gale ------------------------------------------------

function Get-StandaloneSource {
    $cands = @()
    if ($Source) { $cands += $Source }
    $cands += (Join-Path $PSScriptRoot '..\ResoniteMods')                    # installed: {app}\tools, {app}\ResoniteMods
    $cands += (Join-Path $PSScriptRoot '..\bridge\installer\resonite_mods')  # the repository, after staging
    foreach ($c in $cands) {
        $plugin = Join-Path $c "Renderer\BepInEx\plugins\$PluginOnlyStandalone"
        if ((Test-Path -LiteralPath (Join-Path $c 'standalone\Renderer\BepInEx\core\BepInEx.Preloader.dll')) -and (Test-Path -LiteralPath $plugin)) {
            return (Get-FullDir $c)
        }
    }
    return $null
}

function Invoke-Standalone($Build) {
    Say head ''
    Say head "Installing SteamVRRoleFix without Gale into $($Build.Name)  ($($Build.Path))"
    $running = Get-Running $Build.Path
    if ($running.Count) { Say err "$($Build.Name) is running ($($running -join ', '))."; Say next 'Close it, then run this again.'; return $false }
    $old = Get-Record $Build.Path
    if ($old -and $old.mode -eq 'gale') {
        Say err "$($Build.Name) is linked to Gale profile '$(Split-Path -Leaf $old.profile)': SteamVRRoleFix belongs in that profile (setup puts it there)."
        Say next 'Nothing to do here; or -Unlink first to stop using Gale for this build.'
        return $false
    }
    $src = Get-StandaloneSource
    if (-not $src) {
        Say err 'The files for a standalone install were not found (BepInEx for the renderer and SteamVRRoleFix).'
        Say next 'Run CyberFinger Bridge setup with the Resonite mods selected, or pass -Source <folder holding standalone\ and Renderer\BepInEx\plugins\>.'
        return $false
    }
    $rec = New-Record 'standalone' $old
    $r = Join-Path $Build.Path 'Renderer'
    $bep = Join-Path $r 'BepInEx'
    try {
        $own = (Test-Path -LiteralPath (Join-Path $bep 'core\BepInEx.Preloader.dll')) -and -not ($rec.dirs -contains $bep)
        if ($own) {
            Say info 'The renderer already has BepInEx (installed by hand or by a mod manager): adding SteamVRRoleFix to its plugins.'
        } else {
            foreach ($f in @('winhttp.dll', '.doorstop_version')) {
                $s = Join-Path $src "standalone\Renderer\$f"
                if (Test-Path -LiteralPath $s) { Copy-In $s (Join-Path $r $f) $rec }
            }
            Write-Ini (Join-Path $src 'standalone\Renderer\doorstop_config.ini') (Join-Path $r 'doorstop_config.ini') @{ enabled = 'true'; target_assembly = 'BepInEx\core\BepInEx.Preloader.dll' } $rec
            if (-not (Test-Path -LiteralPath $bep)) { New-Item -ItemType Directory -Path $bep | Out-Null; $rec.dirs += $bep }
            New-Item -ItemType Directory -Path (Join-Path $bep 'core') -Force | Out-Null
            foreach ($f in @(Get-ChildItem -LiteralPath (Join-Path $src 'standalone\Renderer\BepInEx\core') -File)) { Copy-In $f.FullName (Join-Path $bep "core\$($f.Name)") $rec }
            Say ok 'Renderer side: BepInEx for the renderer (doorstop, Renderer\doorstop_config.ini enabled=true)'
        }
        $pdest = Join-Path $bep "plugins\$PluginOnlyStandalone"
        if (-not (Test-Path -LiteralPath $pdest)) { New-Item -ItemType Directory -Path $pdest -Force | Out-Null; if (-not ($rec.dirs -contains $pdest)) { $rec.dirs += $pdest } }
        foreach ($f in @(Get-ChildItem -LiteralPath (Join-Path $src "Renderer\BepInEx\plugins\$PluginOnlyStandalone") -File)) { Copy-In $f.FullName (Join-Path $pdest $f.Name) $rec }
        $rec.renderer = $true
        Say ok "SteamVRRoleFix: $pdest"
    } catch {
        Say err "Installing failed: $($_.Exception.Message)"
        Say next "Check that nothing from $($Build.Path) is running and that you can write there, then run this again (or -Unlink to undo what was done)."
        Save-Record $Build.Path $rec
        return $false
    }
    Save-Record $Build.Path $rec
    Set-Index (@(Get-Index) + $Build.Path)
    Say ok "Done. Every change is recorded in $($Build.Path)\$RecordName."
    Say next "Recommended: Gale (https://github.com/Kesomannen/gale) with a profile named '$($Build.Name)' manages mods for this build; link it then (run setup again, or this script without -Standalone)."
    Say next "Troubleshooting: -Mods Off starts it without SteamVRRoleFix; -Unlink removes it:"
    Say info "  resonite_gale_link -Game ""$($Build.Path)"" -Unlink"
    return $true
}

# -- undo, switches, state ------------------------------------------------------

function Invoke-Unlink($Build, [bool]$Quiet = $false) {
    if (-not $Quiet) { Say head ''; Say head "Unlinking $($Build.Name)  ($($Build.Path))" }
    $rec = Get-Record $Build.Path
    if (-not $rec) { Say ok 'Nothing to undo (no record of a link here).'; return $true }
    $running = Get-Running $Build.Path
    if ($running.Count) { Say err "$($Build.Name) is running ($($running -join ', '))."; Say next 'Close it, then run this again.'; return $false }
    foreach ($j in @($rec.junctions)) { if ($j) { Remove-Junction $j } }
    foreach ($f in @($rec.files)) { if ($f -and (Test-Path -LiteralPath $f -PathType Leaf)) { Remove-Item -LiteralPath $f -Force } }
    foreach ($d in @($rec.dirs | Sort-Object Length -Descending)) {   # folders we created, deepest first
        if ($d -and (Test-Path -LiteralPath $d) -and -not (Get-JunctionTarget $d)) { Remove-Item -LiteralPath $d -Recurse -Force }
    }
    foreach ($b in @($rec.backups)) {
        if ($b -and (Test-Path -LiteralPath $b.backup) -and -not (Test-Path -LiteralPath $b.path)) { Move-Item -LiteralPath $b.backup -Destination $b.path }
    }
    foreach ($e in @($rec.iniEdits)) {
        if ($e -and (Test-Path -LiteralPath $e.file) -and $null -ne $e.old) { Set-IniValue $e.file $e.key ([string]$e.old) }
    }
    if ($rec.profile) { Update-Marker $rec.profile $Build.Path $false }
    Remove-Item -LiteralPath (Join-Path $Build.Path $RecordName) -Force
    Set-Index @(Get-Index | Where-Object { $_ -ne $Build.Path })
    if (-not $Quiet) { Say ok "Undone: $($Build.Name) is as it was before, and loads no mods through this link (the Gale profile keeps its mods)." }
    return $true
}

function Set-Switch($Build, [string]$Which, [string]$State) {
    $rec = Get-Record $Build.Path
    if (-not $rec) { Say err "$($Build.Name) isn't linked (no record here)."; Say next 'Link it first (run without -Engine / -Mods).'; return $false }
    $v = if ($State -eq 'On') { 'true' } else { 'false' }
    $done = @()
    $hook = Join-Path $Build.Path 'hookfxr.ini'
    if ($rec.engine -and (Test-Path -LiteralPath $hook)) {
        Set-IniValue $hook 'enable' $v
        if ($rec.profile -and (Test-Path -LiteralPath (Join-Path $rec.profile 'hookfxr.ini'))) { Set-IniValue (Join-Path $rec.profile 'hookfxr.ini') 'enable' $v }
        $done += "hookfxr.ini enable=$v"
    }
    if ($Which -eq 'Mods') {
        $door = Join-Path $Build.Path 'Renderer\doorstop_config.ini'
        if ($rec.renderer -and (Test-Path -LiteralPath $door)) { Set-IniValue $door 'enabled' $v; $done += "Renderer\doorstop_config.ini enabled=$v" }
    }
    if (-not $done.Count) { Say err 'Nothing to switch: no loader of this link found.'; return $false }
    Say ok "$($Build.Name): $($done -join ', ')"
    if ($State -eq 'Off' -and $Which -eq 'Mods') {
        Say next "It now starts without mods. -Mods On loads them again."
        # -LoadAssembly loads RML without BepInEx: those mods aren't switched off with it
        $rml = @(Get-RmlStarters $Build)
        foreach ($s in $rml) { Say warn "$($s.Where) passes -LoadAssembly Libraries/ResoniteModLoader.dll: started from there, it still loads the ResoniteModLoader mods (CyberFingerMod, ProximityGrab)." }
        if ($rml.Count) { Say next 'For no mods at all, take -LoadAssembly out (the supported setup has none: ResoniteModLoaderLoader in the Gale profile loads RML).' }
    }
    elseif ($State -eq 'Off') { Say next "It now starts without engine-side mods, the ResoniteModLoader ones included when ResoniteModLoaderLoader loads them (renderer-side ones such as SteamVRRoleFix still load). -Engine On switches them back." }
    else { Say next 'Its mods load again on the next start.' }
    return $true
}

# One record per build: state, profile, note.
function Get-BuildState($Build, [string]$GaleDir, [bool]$HaveGale) {
    $rec = Get-Record $Build.Path
    if ($rec -and $rec.mode -eq 'standalone') {
        # Once Gale has a profile for it, the build can load that instead: linking replaces the standalone install.
        $prof = if ($HaveGale) { Find-ProfileFor $Build $GaleDir } else { $null }
        if ($prof) {
            $info = Get-ProfileInfo $prof $GaleDir
            if ($info.Engine -or ($info.Renderer -and $info.Doorstop)) {
                return @('ready', $prof, "has SteamVRRoleFix without Gale; can load Gale profile '$($info.Name)' instead (not linked yet)")
            }
        }
        return @('standalone', '', 'SteamVRRoleFix installed without Gale')
    }
    if ($rec) {
        if (-not (Test-Path -LiteralPath $rec.profile)) { return @('broken', $rec.profile, "linked to a Gale profile that no longer exists ($($rec.profile))") }
        $lost = @(@($rec.junctions) | Where-Object { $_ -and -not (Get-JunctionTarget $_) })
        if ($lost.Count) { return @('broken', $rec.profile, "a junction of the link is gone ($($lost -join ', '))") }
        $sides = @()
        if ($rec.engine) { $sides += $(if ((Get-IniValue (Join-Path $Build.Path 'hookfxr.ini') 'enable') -eq 'true') { 'engine' } else { 'engine OFF' }) }
        if ($rec.renderer) { $sides += $(if ((Get-IniValue (Join-Path $Build.Path 'Renderer\doorstop_config.ini') 'enabled') -eq 'true') { 'renderer' } else { 'renderer OFF' }) }
        return @('linked', $rec.profile, "loads Gale profile '$(Split-Path -Leaf $rec.profile)' ($($sides -join ' + '))")
    }
    if (-not $HaveGale) { return @('nogale', '', 'Gale is not installed (recommended for managing mods); SteamVRRoleFix can be installed on its own') }
    $prof = Find-ProfileFor $Build $GaleDir
    $want = if ($Build.Steam) { "'Resonite' or 'Default'" } else { "'$($Build.Name)'" }
    if (-not $prof) { return @('noprofile', '', "no Gale profile named $want (create one in Gale, e.g. by copying your main profile)") }
    $info = Get-ProfileInfo $prof $GaleDir
    if (-not $info.Engine -and -not ($info.Renderer -and $info.Doorstop)) {
        return @('incomplete', $prof, "Gale profile '$($info.Name)' lacks BepisLoader, BepInExRenderer and RenderiteHook (install them in Gale)")
    }
    return @('ready', $prof, "can load Gale profile '$($info.Name)' (not linked yet)")
}

# -- main -----------------------------------------------------------------------

$galeDir = Get-GaleDir
$haveGale = Test-Path -LiteralPath (Join-Path $galeDir 'resonite\profiles')
if ($Game -and -not (Test-ResoniteBuild (Get-FullDir $Game))) {
    Say err "$Game is not a Resonite build (no Renderer\Renderite.Renderer.exe and Renderite.Host)."
    Say next 'Pass the folder that holds the build, or its launcher (.exe).'
    exit 1
}
$shortcuts = Get-Shortcuts
$builds = @(Get-Builds $shortcuts)
if ($Game) { $builds = @($builds | Where-Object { $_.Path -eq (Get-FullDir $Game) }) }

if ($Plan) {   # for setup: state|name|folder|profile|note, one build per line
    foreach ($b in $builds) {
        $s = Get-BuildState $b $galeDir $haveGale
        $line = '{0}|{1}|{2}|{3}|{4}' -f $s[0], $b.Name, $b.Path, $s[1], $s[2]
        if ($Out) { Write-Out $line } else { Write-Output $line }
    }
    exit 0
}

if ($Unlink -and $All) {   # every build linked from this PC: the index, and any build found with a record
    $done = @(Get-Index)
    $failed = 0
    foreach ($p in @($done + @($builds | ForEach-Object { $_.Path }) | Sort-Object -Unique)) {
        if (-not (Test-Path -LiteralPath (Join-Path $p $RecordName))) { continue }
        $b = @($builds | Where-Object { $_.Path -eq $p })[0]
        if (-not $b) { $b = [pscustomobject]@{ Path = $p; Name = (Split-Path -Leaf $p); Steam = $false; Shortcuts = @() } }
        if (-not (Invoke-Unlink $b)) { $failed++ }
    }
    if (-not $failed) { Set-Index @() }
    Say ok $(if ($failed) { "$failed build(s) could not be restored (see above)." } else { 'Every linked build is restored.' })
    exit $(if ($failed) { 1 } else { 0 })
}

if ($Status -or (-not $Game -and -not $script:Interactive -and -not $Unlink -and -not $Engine -and -not $Mods)) {
    Say head "Resonite builds  (Gale data: $(if ($haveGale) { $galeDir } else { 'Gale not found' }))"
    if (-not $builds.Count) { Say info 'None found. Pass -Game <folder> for a build started some other way.' }
    foreach ($b in $builds) {
        $s = Get-BuildState $b $galeDir $haveGale
        $kind = @{ linked = 'ok'; standalone = 'ok'; ready = 'info'; noprofile = 'warn'; incomplete = 'warn'; nogale = 'warn'; broken = 'err' }[$s[0]]
        Say $kind "$($b.Name)  ($($b.Path))"
        Say info "    $($s[2])"
        if ($s[0] -eq 'linked') { Test-Launches $b (Get-ProfileInfo $s[1] $galeDir) }
        if ($s[0] -eq 'broken') { Say next "Link it again: resonite_gale_link -Game ""$($b.Path)""" }
    }
    exit $(if ($script:Errors) { 1 } else { 0 })
}

# Interactive choice of the build
$chosen = $null
if ($Game) { $chosen = $builds[0] }
elseif ($script:Interactive) {
    Say head 'Resonite builds found:'
    for ($i = 0; $i -lt $builds.Count; $i++) {
        $s = Get-BuildState $builds[$i] $galeDir $haveGale
        Write-Host ("  [{0}] {1}  ({2})`n        {3}" -f ($i + 1), $builds[$i].Name, $builds[$i].Path, $s[2])
    }
    $a = Read-Host 'Number of the build, or the folder of another one (Enter to quit)'
    if (-not $a) { exit 0 }
    if ($a -match '^\d+$' -and [int]$a -ge 1 -and [int]$a -le $builds.Count) { $chosen = $builds[[int]$a - 1] }
    elseif (Test-ResoniteBuild (Get-FullDir $a)) { $Game = $a; $chosen = @(Get-Builds $shortcuts | Where-Object { $_.Path -eq (Get-FullDir $a) })[0] }
    else { Say err "$a is not a Resonite build (no Renderer\Renderite.Renderer.exe)."; exit 1 }
}
if (-not $chosen) { Say err 'No build chosen: pass -Game <folder>.'; exit 1 }

if ($Unlink) { if (Invoke-Unlink $chosen) { exit 0 } else { exit 1 } }
if ($Mods) { if (Set-Switch $chosen 'Mods' $Mods) { exit 0 } else { exit 1 } }
if ($Engine) { if (Set-Switch $chosen 'Engine' $Engine) { exit 0 } else { exit 1 } }
if ($Standalone) { if (Invoke-Standalone $chosen) { exit 0 } else { exit 1 } }

if (-not $haveGale) {
    Say err "Gale isn't installed (no Resonite profiles in $galeDir)."
    Say next 'Recommended: install Gale (https://github.com/Kesomannen/gale), add Resonite and make a profile, then run this again. Or install SteamVRRoleFix on its own: -Standalone.'
    exit 1
}
$profilePath = Find-ProfileFor $chosen $galeDir
if (-not $profilePath -and $script:Interactive) {
    $profiles = Get-GaleProfiles $galeDir
    Say head "Gale's Resonite profiles:"
    for ($i = 0; $i -lt $profiles.Count; $i++) { Write-Host ("  [{0}] {1}" -f ($i + 1), (Split-Path -Leaf $profiles[$i])) }
    $a = Read-Host "Number of the profile for $($chosen.Name) (Enter to quit)"
    if ($a -match '^\d+$' -and [int]$a -ge 1 -and [int]$a -le $profiles.Count) { $profilePath = $profiles[[int]$a - 1] }
}
if (-not $profilePath) {
    $want = if ($chosen.Steam) { "'Resonite' (or keep 'Default')" } else { "'$($chosen.Name)'" }
    Say err "No Gale profile for $($chosen.Name)."
    Say next "In Gale, create a profile named $want, for example by copying your main one (a build of its own is best given a profile of its own: it may be older than the mods' target), then run this again; or pass -GaleProfile <name>."
    exit 1
}
if (-not (Confirm-Step "Make $($chosen.Name) load Gale profile '$(Split-Path -Leaf $profilePath)' whenever it starts?")) { exit 0 }
if (Invoke-Link $chosen $profilePath $galeDir) { exit $(if ($script:Errors) { 1 } else { 0 }) } else { exit 1 }
