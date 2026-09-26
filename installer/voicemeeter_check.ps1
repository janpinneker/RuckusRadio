<#
.SYNOPSIS
    Standalone check whether VoiceMeeter (any edition, e.g. Banana) is
    installed on this machine, and which launchable executable it resolves
    to. Mirrors the Pascal Script check embedded in installer\setup.iss
    (IsVoiceMeeterInstalled / GetVoiceMeeterExePath) so the logic is
    documented and testable outside the installer.

.DESCRIPTION
    Detection (any one is sufficient):
      1. Uninstall registry keys (both native and WOW6432Node view) under
         HKLM\Software\...\Uninstall\* for a DisplayName containing
         "VoiceMeeter".
      2. Direct file check across the known VoiceMeeter install locations
         (see Get-VoiceMeeterExePath below).

    Executable resolution (same preference order as setup.iss): Banana
    first, then Potato, then standard, i.e.
      voicemeeterpro_x64.exe, voicemeeterpro.exe,
      voicemeeter8x64.exe, voicemeeter8.exe,
      voicemeeter_x64.exe, voicemeeter.exe
    looked up in:
      %ProgramFiles(x86)%\VB\Voicemeeter
      %ProgramW6432%\VB\Voicemeeter (64-bit Program Files, if present)
      the InstallLocation / DisplayIcon / UninstallString directory of the
      matched Uninstall registry entry, if present

.OUTPUTS
    Writes "VoiceMeeter: installed" / "VoiceMeeter: not installed" plus the
    resolved executable path (or "not found") to stdout, and exits 0 if
    installed, 1 if not.
#>

$ErrorActionPreference = "Stop"

$VoiceMeeterExeCandidates = @(
    "voicemeeterpro_x64.exe",
    "voicemeeterpro.exe",
    "voicemeeter8x64.exe",
    "voicemeeter8.exe",
    "voicemeeter_x64.exe",
    "voicemeeter.exe"
)

function Find-VoiceMeeterExeInDir {
    param([string]$Dir)
    if (-not $Dir) { return $null }
    foreach ($name in $VoiceMeeterExeCandidates) {
        $candidate = Join-Path $Dir $name
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

# Strips a registry DisplayIcon/UninstallString value down to a bare file
# path: drops surrounding quotes / a trailing ",<icon index>" / trailing
# command-line arguments after the executable. Mirrors CleanRegPathValue
# in setup.iss.
function Get-CleanRegPathValue {
    param([string]$Value)
    $trimmed = $Value.Trim()
    if ($trimmed.StartsWith('"')) {
        $trimmed = $trimmed.Substring(1)
        $closeQuote = $trimmed.IndexOf('"')
        if ($closeQuote -ge 0) { $trimmed = $trimmed.Substring(0, $closeQuote) }
        return $trimmed
    }
    $exeIndex = $trimmed.ToLower().IndexOf(".exe")
    if ($exeIndex -ge 0) { return $trimmed.Substring(0, $exeIndex + 4) }
    $comma = $trimmed.IndexOf(",")
    if ($comma -ge 0) { return $trimmed.Substring(0, $comma) }
    return $trimmed
}

function Get-VoiceMeeterRegistryEntry {
    param([string]$UninstallRoot)
    if (-not (Test-Path $UninstallRoot)) { return $null }
    $entries = Get-ItemProperty "$UninstallRoot\*" -ErrorAction SilentlyContinue
    foreach ($entry in $entries) {
        if ($entry.DisplayName -and $entry.DisplayName -match "VoiceMeeter") {
            return $entry
        }
    }
    return $null
}

function Get-VoiceMeeterExePath {
    param($RegistryEntry)

    $dirs = @()
    $pf32 = ${env:ProgramFiles(x86)}
    if ($pf32) { $dirs += (Join-Path $pf32 "VB\Voicemeeter") }

    # 64-bit Program Files, equivalent of Inno's {commonpf64}: ProgramW6432
    # is set on 64-bit Windows regardless of the PowerShell process bitness.
    $pf64 = $env:ProgramW6432
    if ($pf64) { $dirs += (Join-Path $pf64 "VB\Voicemeeter") }

    if ($RegistryEntry) {
        if ($RegistryEntry.InstallLocation) {
            $dirs += $RegistryEntry.InstallLocation.TrimEnd('\')
        }
        if ($RegistryEntry.DisplayIcon) {
            $dirs += (Split-Path (Get-CleanRegPathValue $RegistryEntry.DisplayIcon) -Parent)
        }
        if ($RegistryEntry.UninstallString) {
            $dirs += (Split-Path (Get-CleanRegPathValue $RegistryEntry.UninstallString) -Parent)
        }
    }

    foreach ($dir in $dirs) {
        $found = Find-VoiceMeeterExeInDir $dir
        if ($found) { return $found }
    }
    return $null
}

$regEntry64 = Get-VoiceMeeterRegistryEntry "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
$regEntry32 = Get-VoiceMeeterRegistryEntry "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
$matchedRegEntry = if ($regEntry64) { $regEntry64 } elseif ($regEntry32) { $regEntry32 } else { $null }

$foundByRegistry64 = [bool]$regEntry64
$foundByRegistry32 = [bool]$regEntry32

$exePath = Get-VoiceMeeterExePath -RegistryEntry $matchedRegEntry
$foundByFile = [bool]$exePath

$installed = $foundByRegistry64 -or $foundByRegistry32 -or $foundByFile

Write-Output "Registry check (64-bit Uninstall key): $foundByRegistry64"
Write-Output "Registry check (WOW6432Node Uninstall key): $foundByRegistry32"
if ($exePath) {
    Write-Output "Resolved executable: $exePath"
} else {
    Write-Output "Resolved executable: not found"
}

if ($installed) {
    Write-Output "VoiceMeeter: installed"
    exit 0
} else {
    Write-Output "VoiceMeeter: not installed"
    exit 1
}
