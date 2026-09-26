; Ruckus Radio — Inno Setup installer script.
; Builds installer\Output\RuckusRadioSetup.exe from dist\RuckusRadio.exe
; (onefile PyInstaller build, ffmpeg already bundled inside — not duplicated
; here). Per-user install, no admin rights required for the app itself.
;
; Compile: "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer\setup.iss
; (run from the repo root so the relative Source/OutputDir paths resolve).

#define MyAppName "Ruckus Radio"
#ifndef MyAppVersion
  #define MyAppVersion "1.1.0"
#endif
#define MyAppExeName "RuckusRadio.exe"
#define MyAppId "{{F92283C7-4A3C-4ABB-B59B-4EAE5B67AC23}"

[Setup]
; Script lives in installer\ but dist\/assets\ live one level up in the repo
; root — resolve every relative Source:/OutputDir/SetupIconFile path from there.
SourceDir=..
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppName}
DefaultDirName={localappdata}\Programs\RuckusRadio
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=installer\Output
OutputBaseFilename=RuckusRadioSetup
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; a Ruckus Radio process that ignores the Restart Manager must not abort a silent
; in-app update (suppressed message box = Abort = rollback, no relaunch)
CloseApplications=force

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
german.VMPageCaption=Virtuelles Mikrofon
german.VMPageDesc=VB-CABLE oder VoiceMeeter — eines von beiden braucht Ruckus Radio
german.VMCheckbox=VB-CABLE jetzt herunterladen und installieren (öffnet vb-audio.com im Browser)
german.GuidePageCaption=Einmalige Einrichtung
german.GuidePageDesc=Diese Schritte danach einmalig von Hand erledigen
german.LaunchVoiceMeeter=VoiceMeeter starten
german.LaunchApp=Ruckus Radio starten
german.CreateDesktopIcon=Desktop-Verknüpfung erstellen
english.VMPageCaption=Virtual microphone
english.VMPageDesc=VB-CABLE or VoiceMeeter - Ruckus Radio needs one of them
english.VMCheckbox=Download and install VB-CABLE now (opens vb-audio.com in your browser)
english.GuidePageCaption=One-time setup
english.GuidePageDesc=Do these steps by hand once, afterwards
english.LaunchVoiceMeeter=Start VoiceMeeter
english.LaunchApp=Start Ruckus Radio
english.CreateDesktopIcon=Create a desktop icon

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\RuckusRadio.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "assets\icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "THIRD-PARTY-LICENSES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\icon.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Run]
Filename: "{code:GetVoiceMeeterExePath}"; Description: "{cm:LaunchVoiceMeeter}"; Flags: postinstall nowait skipifsilent skipifdoesntexist runasoriginaluser; Check: VoiceMeeterExeFound
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchApp}"; Flags: postinstall nowait skipifsilent runasoriginaluser
; after a silent in-app update (/UPDATE) Ruckus Radio comes back by itself
Filename: "{app}\{#MyAppExeName}"; Flags: nowait runasoriginaluser; Check: IsUpdateRun

; Deliberately no [UninstallDelete] entries pointing at %APPDATA%\Soundboard —
; the user's config/sounds/icons must survive an uninstall. The default
; uninstaller only removes what [Files]/[Icons] installed, so this is
; already true as long as nothing below references that path.

[Code]
var
  VMPage: TWizardPage;
  VMCheckBox: TNewCheckBox;
  GuidePage: TWizardPage;
  VMAlreadyInstalled: Boolean;
  VMRegSubkeyPath: String;

const
  AppMutexName = 'Local\RuckusRadioSingleInstance';
  UpdateWaitMs = 30000;

function IsUpdateRun: Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/UPDATE') = 0 then
      Result := True;
end;

{ Number of running RuckusRadio.exe processes (WMI); 0 if WMI is unavailable. }
function AppProcessCount: Integer;
var
  Locator, Service, Items: Variant;
begin
  Result := 0;
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('.', 'root\CIMV2');
    Items := Service.ExecQuery('SELECT ProcessId FROM Win32_Process WHERE Name = ''{#MyAppExeName}''');
    Result := Items.Count;
  except
    Result := 0;
  end;
end;

function InitializeSetup: Boolean;
var
  Waited: Integer;
begin
  { In-app update: Ruckus Radio is still shutting down when it starts us. Wait until
    it has released its single-instance mutex AND every RuckusRadio.exe process is
    gone - the onefile exe keeps running for a moment after the mutex is released
    (it cleans up its unpacked files), and a running exe cannot be replaced.
    At most UpdateWaitMs in total; CloseApplications=force handles a leftover. }
  if IsUpdateRun then
  begin
    Waited := 0;
    while CheckForMutexes(AppMutexName) and (Waited < UpdateWaitMs) do
    begin
      Sleep(250);
      Waited := Waited + 250;
    end;
    while (AppProcessCount > 0) and (Waited < UpdateWaitMs) do
    begin
      Sleep(250);
      Waited := Waited + 250;
    end;
    Log(Format('Waited %d ms for Ruckus Radio to exit, %d process(es) left', [Waited, AppProcessCount]));
  end;
  Result := True;
end;

// Strips a registry DisplayIcon/UninstallString value down to a bare file
// path: drops a trailing ",<icon index>", surrounding quotes, and any
// trailing command-line arguments after the executable.
function CleanRegPathValue(S: String): String;
var
  Trimmed: String;
  P: Integer;
begin
  Trimmed := Trim(S);
  if (Length(Trimmed) > 0) and (Trimmed[1] = '"') then
  begin
    Trimmed := Copy(Trimmed, 2, Length(Trimmed) - 1);
    P := Pos('"', Trimmed);
    if P > 0 then
      Trimmed := Copy(Trimmed, 1, P - 1);
  end
  else
  begin
    P := Pos('.exe', Lowercase(Trimmed));
    if P > 0 then
      Trimmed := Copy(Trimmed, 1, P + 3)
    else
    begin
      P := Pos(',', Trimmed);
      if P > 0 then
        Trimmed := Copy(Trimmed, 1, P - 1);
    end;
  end;
  Result := Trimmed;
end;

// Looks for a launchable VoiceMeeter executable directly inside Dir, in
// preference order: Banana (voicemeeterpro*), then Potato (voicemeeter8*),
// then standard (voicemeeter*). Mirrors installer\voicemeeter_check.ps1.
function TryVoiceMeeterExeInDir(Dir: String; var FoundPath: String): Boolean;
var
  Candidates: array[0..5] of String;
  FullPath: String;
  I: Integer;
begin
  Result := False;
  if Dir = '' then
    Exit;

  Candidates[0] := 'voicemeeterpro_x64.exe';
  Candidates[1] := 'voicemeeterpro.exe';
  Candidates[2] := 'voicemeeter8x64.exe';
  Candidates[3] := 'voicemeeter8.exe';
  Candidates[4] := 'voicemeeter_x64.exe';
  Candidates[5] := 'voicemeeter.exe';

  for I := 0 to 5 do
  begin
    FullPath := AddBackslash(Dir) + Candidates[I];
    if FileExists(FullPath) then
    begin
      FoundPath := FullPath;
      Result := True;
      Exit;
    end;
  end;
end;

// Same check as installer\voicemeeter_check.ps1: registry Uninstall keys
// (64-bit + WOW6432Node view) for a DisplayName containing "VoiceMeeter",
// plus a direct file check across the known VoiceMeeter install locations.
// Also remembers the matched Uninstall subkey (VMRegSubkeyPath) so
// GetVoiceMeeterExePath can consult its InstallLocation/DisplayIcon/
// UninstallString values too.
function IsVoiceMeeterInstalled(): Boolean;
var
  Names: TArrayOfString;
  I: Integer;
  DisplayName, DummyPath: String;
  Found: Boolean;
begin
  Found := False;
  VMRegSubkeyPath := '';

  if RegGetSubkeyNames(HKLM, 'Software\Microsoft\Windows\CurrentVersion\Uninstall', Names) then
  begin
    for I := 0 to GetArrayLength(Names) - 1 do
    begin
      if RegQueryStringValue(HKLM, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\' + Names[I], 'DisplayName', DisplayName) then
        if Pos('VOICEMEETER', Uppercase(DisplayName)) > 0 then
        begin
          Found := True;
          VMRegSubkeyPath := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\' + Names[I];
          Break;
        end;
    end;
  end;

  if not Found then
    if RegGetSubkeyNames(HKLM, 'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall', Names) then
    begin
      for I := 0 to GetArrayLength(Names) - 1 do
      begin
        if RegQueryStringValue(HKLM, 'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\' + Names[I], 'DisplayName', DisplayName) then
          if Pos('VOICEMEETER', Uppercase(DisplayName)) > 0 then
          begin
            Found := True;
            VMRegSubkeyPath := 'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\' + Names[I];
            Break;
          end;
      end;
    end;

  if not Found then
  begin
    if TryVoiceMeeterExeInDir(ExpandConstant('{commonpf32}\VB\Voicemeeter'), DummyPath) then
      Found := True
    else if IsWin64 and TryVoiceMeeterExeInDir(ExpandConstant('{commonpf64}\VB\Voicemeeter'), DummyPath) then
      Found := True;
  end;

  Result := Found;
end;

// Resolves an actually launchable VoiceMeeter executable: Banana preferred,
// then Potato, then standard. Checks {commonpf32}\VB\Voicemeeter and (on a
// 64-bit OS) {commonpf64}\VB\Voicemeeter first, then falls back to the
// InstallLocation / DisplayIcon / UninstallString directory of the matched
// Uninstall registry entry (set by IsVoiceMeeterInstalled), for
// installations in a non-default location. Returns '' if nothing is found.
function GetVoiceMeeterExePath(Param: String): String;
var
  Dirs: array[0..4] of String;
  N: Integer;
  FoundPath, RegValue: String;
  I: Integer;
begin
  Result := '';
  N := 0;

  Dirs[N] := ExpandConstant('{commonpf32}\VB\Voicemeeter');
  N := N + 1;

  if IsWin64 then
  begin
    Dirs[N] := ExpandConstant('{commonpf64}\VB\Voicemeeter');
    N := N + 1;
  end;

  if VMRegSubkeyPath <> '' then
  begin
    if RegQueryStringValue(HKLM, VMRegSubkeyPath, 'InstallLocation', RegValue) and (Trim(RegValue) <> '') then
    begin
      Dirs[N] := RemoveBackslashUnlessRoot(Trim(RegValue));
      N := N + 1;
    end;

    if RegQueryStringValue(HKLM, VMRegSubkeyPath, 'DisplayIcon', RegValue) and (Trim(RegValue) <> '') then
    begin
      Dirs[N] := RemoveBackslashUnlessRoot(ExtractFilePath(CleanRegPathValue(RegValue)));
      N := N + 1;
    end;

    if RegQueryStringValue(HKLM, VMRegSubkeyPath, 'UninstallString', RegValue) and (Trim(RegValue) <> '') then
    begin
      Dirs[N] := RemoveBackslashUnlessRoot(ExtractFilePath(CleanRegPathValue(RegValue)));
      N := N + 1;
    end;
  end;

  for I := 0 to N - 1 do
  begin
    if TryVoiceMeeterExeInDir(Dirs[I], FoundPath) then
    begin
      Result := FoundPath;
      Exit;
    end;
  end;
end;

// Drives both the [Run] "VoiceMeeter starten" checkbox visibility and its
// skipifdoesntexist flag: only True when a real, launchable exe was found.
function VoiceMeeterExeFound(): Boolean;
begin
  Result := FileExists(GetVoiceMeeterExePath(''));
end;

// VB-CABLE installs its control panel next to the driver, so one file check per
// Program Files view is enough - it has no launchable "mixer" the way VoiceMeeter has.
function IsCableInstalled(): Boolean;
begin
  Result := FileExists(ExpandConstant('{commonpf32}\VB\CABLE\VBCABLE_ControlPanel.exe'));
  if (not Result) and IsWin64 then
    Result := FileExists(ExpandConstant('{commonpf64}\VB\CABLE\VBCABLE_ControlPanel.exe'));
end;

// Either virtual mic is enough: VB-CABLE (Ruckus Radio mixes) or VoiceMeeter
// (VoiceMeeter mixes). The hint page is only shown when neither exists.
function IsVirtualMicInstalled(): Boolean;
begin
  Result := IsCableInstalled() or IsVoiceMeeterInstalled();
end;

procedure InitializeWizard;
var
  Lbl: TNewStaticText;
  Memo: TNewMemo;
begin
  VMAlreadyInstalled := IsVirtualMicInstalled();

  { Both custom pages are inserted right after the "Installing" page, i.e.
    they appear after the app files are copied and before the Finished page
    — matching plan steps 4 ("VoiceMeeter-Check") and 5 ("Setup-Anleitung"). }
  VMPage := CreateCustomPage(wpInstalling, CustomMessage('VMPageCaption'), CustomMessage('VMPageDesc'));

  Lbl := TNewStaticText.Create(VMPage);
  Lbl.Parent := VMPage.Surface;
  Lbl.Left := 0;
  Lbl.Top := 0;
  Lbl.Width := VMPage.SurfaceWidth;
  Lbl.AutoSize := False;
  Lbl.WordWrap := True;
  Lbl.Height := ScaleY(110);
  Lbl.Caption :=
    'Ruckus Radio braucht ein virtuelles Mikrofon, um Sounds in Discord/Steam einzuspielen. ' +
    'Auf diesem PC wurde weder VB-CABLE noch VoiceMeeter gefunden.' + #13#10#13#10 +
    'Empfohlen ist VB-CABLE: Ruckus Radio mischt dein Mikrofon dann selbst dazu, du musst nichts routen.' + #13#10 +
    'https://vb-audio.com/Cable/' + #13#10#13#10 +
    'Der offizielle Download ist ein ZIP-Archiv, das dieser Installer nicht automatisch entpacken und starten kann ' +
    '— deshalb öffnet der Haken unten die offizielle Download-Seite in deinem Browser. ' +
    'Dort das ZIP herunterladen, entpacken und VBCABLE_Setup_x64.exe ausführen (Neustart nötig). ' +
    'Kein Internet gerade? Diese Seite später von Hand im Browser öffnen.';

  VMCheckBox := TNewCheckBox.Create(VMPage);
  VMCheckBox.Parent := VMPage.Surface;
  VMCheckBox.Left := 0;
  VMCheckBox.Top := Lbl.Top + Lbl.Height + ScaleY(12);
  VMCheckBox.Width := VMPage.SurfaceWidth;
  VMCheckBox.Caption := CustomMessage('VMCheckbox');
  VMCheckBox.Checked := True;

  GuidePage := CreateCustomPage(VMPage.ID, CustomMessage('GuidePageCaption'), CustomMessage('GuidePageDesc'));
  Memo := TNewMemo.Create(GuidePage);
  Memo.Parent := GuidePage.Surface;
  Memo.Left := 0;
  Memo.Top := 0;
  Memo.Width := GuidePage.SurfaceWidth;
  Memo.Height := GuidePage.SurfaceHeight;
  Memo.ScrollBars := ssVertical;
  Memo.ReadOnly := True;
  Memo.Font.Name := 'Segoe UI';
  Memo.Text :=
    'MIT VB-CABLE (empfohlen, Ruckus Radio mischt selbst):' + #13#10#13#10 +
    '1. VB-CABLE von vb-audio.com/Cable installieren, PC neu starten.' + #13#10#13#10 +
    '2. Ruckus Radio starten. Unten links muss "VB-CABLE verbunden" stehen.' + #13#10#13#10 +
    '3. Unten rechts "Discord-Gerät" klicken — der exakte Gerätename liegt dann in der Zwischenablage.' + #13#10#13#10 +
    '4. Discord → Einstellungen → Sprache & Video: Eingabegerät = CABLE Output (VB-Audio Virtual Cable).' + #13#10 +
    '    Steam → Einstellungen → Sprache: dasselbe Gerät.' + #13#10#13#10 +
    '5. Discord: Rauschunterdrückung und Echounterdrückung AUS — sonst filtert Discord die Sounds weg.' + #13#10#13#10 +
    '6. Unten rechts "Prüfen" klicken: Ruckus Radio schickt einen Ton durch das Kabel und misst, ob er ankommt.' + #13#10#13#10 +
    '7. Wichtig: Discord hört dein Mikrofon nur, solange Ruckus Radio läuft. Im Assistenten gibt es dafür ' +
    '"Mit Windows starten".' + #13#10#13#10#13#10 +
    'ODER MIT VOICEMEETER (VoiceMeeter mischt):' + #13#10#13#10 +
    '1. VoiceMeeter installieren, PC neu starten.' + #13#10#13#10 +
    '2. Hardware Input 1 → echtes Mikrofon auswählen, Fader auf 0 dB, nicht gemutet.' + #13#10#13#10 +
    '3. A1 → deine Kopfhörer. Ohne Gerät auf A1 läuft VoiceMeeters Engine nicht und der B-Bus bleibt stumm.' + #13#10#13#10 +
    '4. Hardware Input 1 UND Virtual Input beide auf BUS B routen (Klick auf die "B"-Kachel bei beiden Kanälen).' + #13#10#13#10 +
    '5. Audio-Engine auf 48000 Hz stellen (Ruckus Radio nutzt denselben Sample-Rate).' + #13#10#13#10 +
    '6. Discord und Steam: Eingabegerät = Voicemeeter Out B1. Nicht "Standard", nicht B2/B3 — die füttert nur ' +
    'die Potato-Edition. Der Knopf "Discord-Gerät" im Dock nennt den richtigen Namen.' + #13#10#13#10 +
    '7. VoiceMeeter muss laufen, bevor Ruckus Radio gestartet wird — die App sucht das Gerät beim Start.';
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;

  { Neither custom page has any effect on the files that get installed, but
    they must never appear (and must never trigger a browser popup) during
    an unattended /VERYSILENT or /SILENT run. }
  if WizardSilent then
  begin
    if (PageID = VMPage.ID) or (PageID = GuidePage.ID) then
      Result := True;
    Exit;
  end;

  if (PageID = VMPage.ID) and VMAlreadyInstalled then
    Result := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  ErrorCode: Integer;
begin
  Result := True;
  if WizardSilent then
    Exit;
  if (CurPageID = VMPage.ID) and (not VMAlreadyInstalled) and VMCheckBox.Checked then
    ShellExec('open', 'https://vb-audio.com/Cable/', '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
end;
