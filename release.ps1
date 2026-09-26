<#
.SYNOPSIS
    Baut und veroeffentlicht eine Ruckus-Radio-Version auf GitHub (janpinneker/RuckusRadio).

.DESCRIPTION
    Ohne -Publish verlaesst nichts den Rechner: Version setzen, Tests, exe + Installer
    bauen, Pruefsumme, gefilterte Momentaufnahme nach ..\RuckusRadio-public (eigenes
    Git-Repo, Commits nur unter der GitHub-noreply-Adresse). Mit -Publish zusaetzlich
    Push, Tag und GitHub-Release mit Installer + Pruefsumme.

    Die Private-Daten-Pruefung liest ihre Suchbegriffe aus release-private.txt neben
    diesem Skript (ein Begriff pro Zeile, von Git ignoriert) - so stehen die Begriffe
    selbst nie in der oeffentlichen Momentaufnahme. Fehlt die Datei, bricht das Skript ab.
#>
#Requires -Version 7
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Notes = "",
    [switch]$Publish
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$python = Join-Path $root "venv\Scripts\python.exe"
$repo = "janpinneker/RuckusRadio"
$owner = $repo.Split("/")[0]
$public = Join-Path (Split-Path $root -Parent) "RuckusRadio-public"
$allow = @("soundboard", "tests", "assets/icon.ico", "build.ps1", "build.spec", "release.ps1",
           "installer/setup.iss", "installer/voicemeeter_check.ps1", "requirements.txt",
           "run.py", "README.md", ".gitignore", "THIRD-PARTY-LICENSES.md")
$privateFile = Join-Path $root "release-private.txt"
$testTimeoutMs = 150000

function Fail($message) { Write-Host $message -ForegroundColor Red; exit 1 }

# Every file, text or binary (audio/image metadata!), read as Latin-1, UTF-8 and UTF-16LE (at
# both byte alignments), so ASCII, UTF-8 and UTF-16 text (e.g. ID3 tags) are all searched.
# $allowed: exact strings that may appear (the public repo path) - removed before searching.
function Find-PrivateData($dir, $terms, $allowed) {
    Get-ChildItem $dir -Recurse -File -Force | Where-Object { $_.FullName -notlike "*\.git\*" } | ForEach-Object {
        $bytes = [IO.File]::ReadAllBytes($_.FullName)
        $texts = @([Text.Encoding]::Latin1.GetString($bytes), [Text.Encoding]::UTF8.GetString($bytes), [Text.Encoding]::Unicode.GetString($bytes))
        if ($bytes.Length -gt 1) { $texts += [Text.Encoding]::Unicode.GetString($bytes, 1, $bytes.Length - 1) }
        foreach ($ok in $allowed) {
            $texts = @($texts | ForEach-Object { $_ -ireplace [regex]::Escape($ok), "" })
        }
        foreach ($term in $terms) {
            if ($texts | Where-Object { $_.IndexOf($term, [StringComparison]::OrdinalIgnoreCase) -ge 0 }) {
                "$($_.FullName.Substring($dir.Length + 1)): $term"
            }
        }
    }
}

# private-data terms: generic user-profile paths - plain, Python-escaped, with slashes -
# (split so this script does not match its own check) + the local, git-ignored list
if (-not (Test-Path $privateFile)) { Fail "release-private.txt fehlt (ein privater Suchbegriff pro Zeile)" }
$generic = @(("C:\" + "Users\"), ("C:\\" + "Users"), ("C:/" + "Users/"))  # parens: "," binds before "+"
$private = $generic + @(Get-Content $privateFile -Encoding utf8 |
    ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith("#") })
if ($private.Count -le $generic.Count) { Fail "release-private.txt enthaelt keine Suchbegriffe" }

# 1-2: clean tree, version must not go down
if ($Version -notmatch '^\d+\.\d+\.\d+$') { Fail "Version muss X.Y.Z sein: $Version" }
$dirty = git status --porcelain --untracked-files=no
if ($dirty) { Fail "Uncommittete Aenderungen - erst committen:`n$dirty" }
$versionFile = Join-Path $root "soundboard\version.py"
$current = (Select-String -Path $versionFile -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
$cmp = ([version]$Version).CompareTo([version]$current)
if ($cmp -lt 0) { Fail "Version $Version ist kleiner als $current" }
if ($Publish) {
    gh release view "v$Version" --repo $repo *> $null
    if ($LASTEXITCODE -eq 0) { Fail "Release v$Version gibt es schon auf GitHub" }
}

# 3: tests (without *_manual.py: those need hardware and hang), each with a time limit -
# before the version bump, so a failing test never leaves a never-released "chore: version X"
# commit behind. Tests don't depend on the version value.
Get-ChildItem tests -Filter "test_*.py" | Where-Object { $_.Name -notlike "*_manual.py" } | ForEach-Object {
    $proc = Start-Process -FilePath $python -ArgumentList "`"$($_.FullName)`"" -NoNewWindow -PassThru `
        -RedirectStandardOutput "$env:TEMP\ruckus-release-test.out" -RedirectStandardError "$env:TEMP\ruckus-release-test.err"
    $null = $proc.Handle  # keeps ExitCode readable after WaitForExit(timeout)
    if (-not $proc.WaitForExit($testTimeoutMs)) {
        $proc.Kill($true)
        Fail "Test haengt (> $($testTimeoutMs / 1000) s): $($_.Name)"
    }
    if ($proc.ExitCode -ne 0) {
        $tail = Get-Content "$env:TEMP\ruckus-release-test.out", "$env:TEMP\ruckus-release-test.err" -Tail 20 | Out-String
        Fail "Test fehlgeschlagen: $($_.Name)`n$tail"
    }
}
& $python run.py --selftest
if ($LASTEXITCODE -ne 0) { Fail "Selbsttest fehlgeschlagen" }

# 4: set version
if ($cmp -gt 0) {
    (Get-Content $versionFile -Raw) -replace '__version__ = ".+"', "__version__ = `"$Version`"" |
        Set-Content $versionFile -NoNewline -Encoding utf8
    git add soundboard/version.py
    git commit -q -m "chore: version $Version" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
    if ($LASTEXITCODE -ne 0) { Fail "Versions-Commit fehlgeschlagen" }
}

# 5-6: build, installer, checksum
& powershell -ExecutionPolicy Bypass -File (Join-Path $root "build.ps1")
if ($LASTEXITCODE -ne 0) { Fail "build.ps1 fehlgeschlagen" }
$iscc = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
& $iscc "/DMyAppVersion=$Version" installer\setup.iss | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "Installer-Build fehlgeschlagen" }
$setup = Join-Path $root "installer\Output\RuckusRadioSetup.exe"
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
"$hash  RuckusRadioSetup.exe" | Set-Content "$setup.sha256" -NoNewline -Encoding ascii

# 7: public snapshot - only files Git tracks under the allow list (no caches, no test output)
if (-not (Test-Path $public)) {
    New-Item -ItemType Directory -Path $public | Out-Null
    git -C $public init -q -b main
}
Get-ChildItem $public -Force | Where-Object { $_.Name -ne ".git" } | Remove-Item -Recurse -Force
foreach ($item in $allow) {
    $files = @(git ls-files -- $item)
    if (-not $files) { Fail "Fehlt fuer die Momentaufnahme (nicht in Git): $item" }
    foreach ($file in $files) {
        $target = Join-Path $public $file
        New-Item -ItemType Directory -Force -Path (Split-Path $target -Parent) | Out-Null
        Copy-Item (Join-Path $root $file) $target -Force
    }
}
$leaks = @(Find-PrivateData $public $private @($repo))
if ($leaks) { Fail "Private Daten in der Momentaufnahme:`n$($leaks -join "`n")" }
$userId = gh api user --jq .id
$login = gh api user --jq .login
if (-not $userId -or -not $login) { Fail "gh ist nicht angemeldet" }
if ($login -ne $owner) { Fail "gh ist als $login angemeldet, das Repo gehoert $owner" }
$noreply = "$userId+$login@users.noreply.github.com"
git -C $public add -A
git -C $public diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git -C $public -c "user.name=$login" -c "user.email=$noreply" commit -q -m "Ruckus Radio $Version"
    if ($LASTEXITCODE -ne 0) { Fail "Commit der Momentaufnahme fehlgeschlagen" }
}
# GIT_AUTHOR_*/GIT_COMMITTER_* variables would beat -c: check what really landed
$identity = git -C $public log -1 --format="%an <%ae>|%cn <%ce>"
if ($identity -ne "$login <$noreply>|$login <$noreply>") { Fail "Falsche Identitaet im oeffentlichen Commit: $identity" }
Write-Output "Momentaufnahme: $public"

if (-not $Publish) {
    Write-Output "Bereit - mit -Publish hochladen. Installer: $setup"
    exit 0
}

# 8: publish (only on the user's explicit go)
gh repo view $repo *> $null
if ($LASTEXITCODE -ne 0) {
    gh repo create $repo --public --description "Ruckus Radio - Soundboard fuer Discord und Spiele" | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "Repo konnte nicht angelegt werden" }
}
$originUrl = "https://github.com/$repo.git"
if (-not (git -C $public remote)) {
    git -C $public remote add origin $originUrl
    if ($LASTEXITCODE -ne 0) { Fail "Origin konnte nicht gesetzt werden" }
} else {
    $currentOrigin = git -C $public remote get-url origin
    if ($currentOrigin -ne $originUrl) { Fail "Falsches Origin-Remote in der Momentaufnahme: $currentOrigin (erwartet $originUrl)" }
}
gh auth setup-git
if ($LASTEXITCODE -ne 0) { Fail "gh auth setup-git fehlgeschlagen" }
git -C $public push -u origin main
if ($LASTEXITCODE -ne 0) { Fail "Push fehlgeschlagen" }
$head = git -C $public rev-parse HEAD
$tagged = git -C $public rev-parse -q --verify "refs/tags/v$Version^{commit}"
if ($tagged -and $tagged -ne $head) { Fail "Tag v$Version zeigt auf einen alten Stand ($tagged) - erst loeschen" }
if (-not $tagged) {
    git -C $public tag "v$Version"
    if ($LASTEXITCODE -ne 0) { Fail "Tag konnte nicht angelegt werden" }
}
git -C $public push origin "v$Version"
if ($LASTEXITCODE -ne 0) { Fail "Tag-Push fehlgeschlagen" }
if (-not $Notes) { $Notes = "Ruckus Radio $Version" }
gh release create "v$Version" $setup "$setup.sha256" --repo $repo --title "Ruckus Radio $Version" --notes $Notes
if ($LASTEXITCODE -ne 0) {
    Fail "Release konnte nicht angelegt werden. Nur falls es auf GitHub halb angelegt ist (ohne beide Dateien): gh release delete v$Version --repo $repo, dann neu starten"
}
Write-Output "Veroeffentlicht: https://github.com/$repo/releases/tag/v$Version"
