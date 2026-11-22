; ── CyberFinger Bridge + SteamVR driver installer ──
; Inno Setup 6.3+ script
; Build everything with tools\build_installer.cmd, or compile with: iscc setup.iss (from installer/ directory)
; after building the SteamVR driver (tools\build_driver.cmd) and the bridge (bridge\build.bat), and optionally
; staging the Resonite mods (tools\stage_resonite_mods.py).

#define MyAppName "CyberFinger Bridge"
#define MyAppVersion "2.2.5"
#define MyAppPublisher "SciCortex Technologies Corp."
#define MyAppURL "https://github.com/DrSciCortex/CyberFinger"
#define MyAppExeName "CyberFingerBridge.exe"
#define ViGEmSetup "ViGEmBus_1.22.0_x64_x86_arm64.exe"
#define HaveViGEm FileExists(AddBackslash(SourcePath) + ViGEmSetup)
#define DriverSource AddBackslash(SourcePath) + "..\..\out\build\x64-Release\driver\cyberfinger"
#define HaveResoniteMods DirExists(AddBackslash(SourcePath) + "resonite_mods")

#if !FileExists(DriverSource + "\bin\win64\driver_cyberfinger.dll")
  #error The SteamVR driver is not built. Run tools\build_driver.cmd first.
#endif
#if !FileExists(AddBackslash(SourcePath) + "..\dist\" + MyAppExeName)
  #error The bridge is not built. Run bridge\build.bat first.
#endif
#if !HaveViGEm
  #pragma message "ViGEmBus installer not found next to setup.iss: building without it (Gamepad mode users install ViGEmBus themselves)."
#endif
#if !HaveResoniteMods
  #pragma message "resonite_mods not found next to setup.iss (tools\stage_resonite_mods.py): building without the Resonite mods."
#endif

[Setup]
; CyberFinger Bridge's own id. Up to 2.2.5 it was a template placeholder ({A1B2C3D4-...}); setup removes such an
; install first (see LegacyAppKey).
AppId={{6F55D1B7-5BEC-42C7-BDA8-01144FD1CF7B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\CyberFinger Bridge
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=CyberFingerBridge_Setup_{#MyAppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
; The SteamVR driver is 64-bit only. Setup itself stays in 32-bit install mode, as in earlier versions:
; 64-bit mode would look for the previous install in the other registry view, miss it, and install a
; second copy next to it instead of upgrading it.
ArchitecturesAllowed=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "steamvrdriver"; Description: "Install the CyberFinger SteamVR driver (controllers + hand skeleton)"; GroupDescription: "SteamVR:"
#if HaveResoniteMods
Name: "resonite"; Description: "Install CyberFinger mods for Resonite"; GroupDescription: "Resonite:"; Check: AnyResonite
Name: "resonite\morefluxactions"; Description: "MoreFluxActions: Flux Actions 1-42 for your ProtoFlux, and the right pink button's mute"; Check: AnyResonite
Name: "resonite\steamvrrolefix"; Description: "SteamVRRoleFix: hands and buttons follow switches to the Quest controllers and back"; Check: AnyResonite
Name: "resonite\cyberfingermod"; Description: "CyberFingerMod: movement follows the controller in use, the laser stays where your avatar puts it"; Check: AnyResonite
Name: "resonite\proximitygrab"; Description: "ProximityGrab: grab with a fist, precision grab with a pinch"; Check: AnyResonite
#endif
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
#if HaveViGEm
Name: "installvigem"; Description: "Install ViGEmBus driver (required for Gamepad mode)"; GroupDescription: "Drivers:"; Flags: checkedonce
#endif

[Files]
; Main application
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

; Icon
Source: "..\assets\icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\icon.png"; DestDir: "{app}"; Flags: ignoreversion

; SteamVR driver folder (registered with SteamVR below)
Source: "{#DriverSource}\*"; DestDir: "{app}\SteamVR\cyberfinger"; Flags: ignoreversion recursesubdirs createallsubdirs; Tasks: steamvrdriver

; Links Resonite builds to Gale profiles: setup, the Start menu entry and the uninstaller run it (README, "Loading Gale
; mods without Gale's launcher")
Source: "..\..\tools\resonite_gale_link.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "..\..\tools\resonite_gale_link.cmd"; DestDir: "{app}\tools"; Flags: ignoreversion

#if HaveResoniteMods
; Resonite mods (tools\stage_resonite_mods.py), laid out as installed: kept here, and copied into the game and its
; mod profiles at the end of setup (InstallResoniteMods). Also the files to copy by hand for another mod manager.
Source: "resonite_mods\VERSIONS.txt"; DestDir: "{app}\ResoniteMods"; Flags: ignoreversion; Tasks: resonite
Source: "resonite_mods\BepInEx\plugins\DrSciCortex-MoreFluxActions\*"; DestDir: "{app}\ResoniteMods\BepInEx\plugins\DrSciCortex-MoreFluxActions"; Flags: ignoreversion recursesubdirs createallsubdirs; Tasks: resonite\morefluxactions
Source: "resonite_mods\Renderer\BepInEx\plugins\DrSciCortex-MoreFluxActions\*"; DestDir: "{app}\ResoniteMods\Renderer\BepInEx\plugins\DrSciCortex-MoreFluxActions"; Flags: ignoreversion recursesubdirs createallsubdirs; Tasks: resonite\morefluxactions
Source: "resonite_mods\Renderer\BepInEx\plugins\DrSciCortex-SteamVRRoleFix\*"; DestDir: "{app}\ResoniteMods\Renderer\BepInEx\plugins\DrSciCortex-SteamVRRoleFix"; Flags: ignoreversion recursesubdirs createallsubdirs; Tasks: resonite\steamvrrolefix
Source: "resonite_mods\rml_mods\CyberFingerMod.dll"; DestDir: "{app}\ResoniteMods\rml_mods"; Flags: ignoreversion; Tasks: resonite\cyberfingermod
Source: "resonite_mods\rml_mods\ProximityGrab.dll"; DestDir: "{app}\ResoniteMods\rml_mods"; Flags: ignoreversion; Tasks: resonite\proximitygrab
; BepInEx for the renderer and its doorstop: SteamVRRoleFix in builds without Gale (the link script's -Standalone)
Source: "resonite_mods\standalone\*"; DestDir: "{app}\ResoniteMods\standalone"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist; Tasks: resonite\steamvrrolefix
#endif

#if HaveViGEm
; ViGEmBus installer — bundled into the setup, extracted on demand
; Download from: https://github.com/nefarius/ViGEmBus/releases/download/v1.22.0/ViGEmBus_1.22.0_x64_x86_arm64.exe
; Place in: installer/ViGEmBus_1.22.0_x64_x86_arm64.exe
Source: "{#ViGEmSetup}"; DestDir: "{tmp}"; Flags: ignoreversion deleteafterinstall; Tasks: installvigem
#endif

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\icon.ico"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{group}\Link Resonite builds to Gale profiles"; Filename: "{app}\tools\resonite_gale_link.cmd"; WorkingDir: "{app}\tools"; Check: AnyResonite
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Run]
; Register the SteamVR driver for the user who started setup (vrpathreg writes %LOCALAPPDATA%\openvr).
; Any other "cyberfinger" registration (e.g. a development build) is removed first.
Filename: "{code:GetVRPathReg}"; Parameters: "removedriverswithname cyberfinger"; Flags: runhidden waituntilterminated runasoriginaluser; Tasks: steamvrdriver; Check: HaveVRPathReg
Filename: "{code:GetVRPathReg}"; Parameters: "adddriver ""{app}\SteamVR\cyberfinger"""; StatusMsg: "Registering the SteamVR driver..."; Flags: runhidden waituntilterminated runasoriginaluser; Tasks: steamvrdriver; Check: HaveVRPathReg

#if HaveViGEm
; Install ViGEmBus driver if the task is selected
Filename: "{tmp}\{#ViGEmSetup}"; Parameters: "/qn /norestart"; StatusMsg: "Installing ViGEmBus driver..."; Tasks: installvigem; Flags: waituntilterminated shellexec
#endif

; Launch after install
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{code:GetVRPathReg}"; Parameters: "removedriver ""{app}\SteamVR\cyberfinger"""; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "UnregisterSteamVRDriver"; Tasks: steamvrdriver

[UninstallDelete]
; The list of the Resonite mods' copies, which the uninstaller removes first (UninstallResoniteMods)
Type: files; Name: "{app}\ResoniteMods\installed.txt"
Type: files; Name: "{app}\ResoniteMods\backups.txt"

[Code]
// ── SteamVR ────────────────────────────────────────────────────────────────

// First path of the Key list ("runtime", "config") in %LOCALAPPDATA%\openvr\openvrpaths.vrpath, or ''.
function ReadOpenVRPath(Key: String): String;
var
  Json: AnsiString;
  Text, Path: String;
  P, Q: Integer;
begin
  Result := '';
  if not LoadStringFromFile(ExpandConstant('{localappdata}\openvr\openvrpaths.vrpath'), Json) then
    Exit;
  Text := Utf8Decode(Json);
  P := Pos('"' + Key + '"', Text);
  if P = 0 then
    Exit;
  Delete(Text, 1, P + Length(Key) + 1);   // drop everything up to and including "<Key>"
  P := Pos('"', Text);                    // opening quote of the first path
  if P = 0 then
    Exit;
  Delete(Text, 1, P);
  Q := Pos('"', Text);
  if Q = 0 then
    Exit;
  Path := Copy(Text, 1, Q - 1);
  StringChangeEx(Path, '\\', '\', True);
  Result := Path;
end;

// SteamVR's install folder, or ''.
function FindSteamVR(): String;
var
  Candidate, SteamPath: String;
begin
  Result := '';
  Candidate := ReadOpenVRPath('runtime');
  if (Candidate <> '') and FileExists(Candidate + '\bin\win64\vrpathreg.exe') then
  begin
    Result := Candidate;
    Exit;
  end;
  if RegQueryStringValue(HKCU, 'Software\Valve\Steam', 'SteamPath', SteamPath) then
  begin
    StringChangeEx(SteamPath, '/', '\', True);
    Candidate := SteamPath + '\steamapps\common\SteamVR';
    if FileExists(Candidate + '\bin\win64\vrpathreg.exe') then
    begin
      Result := Candidate;
      Exit;
    end;
  end;
  Candidate := ExpandConstant('{commonpf32}\Steam\steamapps\common\SteamVR');
  if FileExists(Candidate + '\bin\win64\vrpathreg.exe') then
    Result := Candidate;
end;

function FindVRPathReg(): String;
begin
  Result := FindSteamVR();
  if Result <> '' then
    Result := Result + '\bin\win64\vrpathreg.exe';
end;

function HaveVRPathReg(): Boolean;
begin
  Result := FindVRPathReg() <> '';
end;

function GetVRPathReg(Param: String): String;
begin
  Result := FindVRPathReg();
  if Result = '' then
    Result := 'vrpathreg.exe';            // not found: the run entry fails harmlessly
end;

function IsProcessRunning(Name: String): Boolean;
var
  Locator, Service, Items: Variant;
begin
  Result := False;
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('.', 'root\CIMV2');
    Items := Service.ExecQuery('SELECT ProcessId FROM Win32_Process WHERE Name = ''' + Name + '''');
    Result := Items.Count > 0;
  except
    Result := False;
  end;
end;

function IsSteamVRRunning(): Boolean;
begin
  Result := IsProcessRunning('vrserver.exe');
end;

// SteamVR holds the driver DLL and reads the registration at startup: it must be closed.
function WaitForSteamVRClosed(): Boolean;
begin
  Result := True;
  while IsSteamVRRunning() do
  begin
    if SuppressibleMsgBox('SteamVR is running. Please quit SteamVR, then click Retry.',
                          mbError, MB_RETRYCANCEL, IDCANCEL) = IDCANCEL then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

// ── Resonite mods ──────────────────────────────────────────────────────────
// BepInEx plugins (MoreFluxActions, SteamVRRoleFix) load from the BepInEx that Resonite starts with. With Gale,
// that's the profile's: Gale starts Resonite with --bepinex-target <profile>\BepInEx and points the renderer's
// doorstop at <profile>\Renderer\BepInEx (the game folder then holds only the doorstop and a loader, a
// Renderer\BepInEx\core included). Without a mod manager, BepisLoader is installed in the game folder itself: its
// BepInEx\core. The Resonite plugins page lists both kinds; the user picks. ResoniteModLoader mods (CyberFingerMod,
// ProximityGrab) always go into the game folder's rml_mods: RML loads them from there, also under Gale (where
// ResoniteModLoaderLoader loads the game folder's RML, or the -LoadAssembly launch option does: Gale launches through
// Steam, which adds the game's launch options. Never both: RML loaded twice stops Resonite as it starts).

//
// Resonite builds (the Steam copy, and others such as a private universe) can load a Gale profile's mods
// however they start, without Gale's launcher: tools\resonite_gale_link.ps1 points their loaders at the profile
// (README, "Loading Gale mods without Gale's launcher"). Setup asks it for the builds it finds (-Plan), links the
// ones ticked on the "Resonite builds" page, or without Gale installs SteamVRRoleFix on its own (-Standalone).
// Every change is recorded in the build, and the uninstaller undoes them all (-Unlink -All).

const
  ResoniteModsList = 'installed.txt';     // in {app}\ResoniteMods: every copy made, for the uninstaller
  ResoniteBackupList = 'backups.txt';     // in {app}\ResoniteMods: "path|backup" of what the copies replaced
  LinkScript = 'resonite_gale_link.ps1';

var
  ResoniteLookedUp: Boolean;
  ResoniteDir: String;                    // Resonite's install folder, or ''
  ModTargets, ModTargetNames: TStringList; // folders with BepisLoader (Gale profiles, the game), and their names
  ModTargetIsGale: TStringList;           // '1' for a Gale profile
  ModTargetDefault: Integer;              // the Gale profile last used, or -1
  TargetPage: TInputOptionWizardPage;
  PlanLoaded: Boolean;
  BState, BName, BPath, BProfile, BNote: TStringList;  // the builds found: state, name, folder, profile, note
  BuildsPage: TInputOptionWizardPage;
  BuildItem: TStringList;                 // the build of each item on BuildsPage

// Resonite's install folder, or ''.
function FindResonite(): String;
var
  SteamPath, Candidate, Line: String;
  Lines: TArrayOfString;
  I, P: Integer;
begin
  Result := '';
  if RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Steam App 2519830',
                         'InstallLocation', Candidate) and FileExists(Candidate + '\Resonite.exe') then
  begin
    Result := Candidate;
    Exit;
  end;
  if RegQueryStringValue(HKCU, 'Software\Valve\Steam', 'SteamPath', SteamPath) then
  begin
    StringChangeEx(SteamPath, '/', '\', True);
    // Every Steam library: the "path" entries of libraryfolders.vdf (the Steam folder is one of them)
    if LoadStringsFromFile(SteamPath + '\steamapps\libraryfolders.vdf', Lines) then
      for I := 0 to GetArrayLength(Lines) - 1 do
      begin
        Line := Trim(Lines[I]);
        if Pos('"path"', Line) <> 1 then
          Continue;
        Delete(Line, 1, 6);
        P := Pos('"', Line);
        if P = 0 then
          Continue;
        Delete(Line, 1, P);
        P := Pos('"', Line);
        if P = 0 then
          Continue;
        Candidate := Copy(Line, 1, P - 1);
        StringChangeEx(Candidate, '\\', '\', True);
        Candidate := Candidate + '\steamapps\common\Resonite';
        if FileExists(Candidate + '\Resonite.exe') then
        begin
          Result := Candidate;
          Exit;
        end;
      end;
  end;
  Candidate := ExpandConstant('{commonpf32}\Steam\steamapps\common\Resonite');
  if FileExists(Candidate + '\Resonite.exe') then
    Result := Candidate;
end;

function GetResoniteDir(): String;
begin
  if not ResoniteLookedUp then
  begin
    ResoniteDir := FindResonite();
    ResoniteLookedUp := True;
  end;
  Result := ResoniteDir;
end;

function ResoniteFound(): Boolean;
begin
  Result := GetResoniteDir() <> '';
end;

function IsDirectory(const F: TFindRec): Boolean;
begin
  Result := ((F.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0) and (F.Name <> '.') and (F.Name <> '..');
end;

// 64-bit Windows PowerShell (setup runs 32-bit; the script reads 64-bit processes' paths).
function PowerShellExe(): String;
begin
  Result := ExpandConstant('{sysnative}\WindowsPowerShell\v1.0\powershell.exe');
  if not FileExists(Result) then
    Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

// The text up to the next '|' of S, which loses it.
function NextField(var S: String): String;
var
  P: Integer;
begin
  P := Pos('|', S);
  if P = 0 then
  begin
    Result := S;
    S := '';
  end
  else
  begin
    Result := Copy(S, 1, P - 1);
    Delete(S, 1, P);
  end;
end;

// The lines of a report the link script wrote (-Out, UTF-8), blank ones left out.
function ReadReport(FileName: String): TStringList;
var
  Raw: AnsiString;
  Text: String;
  I: Integer;
begin
  Result := TStringList.Create;
  if not LoadStringFromFile(FileName, Raw) then
    Exit;
  Text := Utf8Decode(Raw);
  if (Length(Text) > 0) and (Ord(Text[1]) = $FEFF) then
    Delete(Text, 1, 1);
  Result.Text := Text;
  for I := Result.Count - 1 downto 0 do
    if Trim(Result.Strings[I]) = '' then
      Result.Delete(I);
end;

// Runs the link script with Params (its report goes to OutFile). It runs elevated, as setup does: for the same user,
// whose Gale profiles and Steam settings setup reads anyway. (ExecAsOriginalUser could not write the report: elevated
// setup's {tmp} is read-only to the user's non-elevated processes.) Returns the script's exit code, -1 if it didn't run.
function RunLinkScript(Script, Params, OutFile: String): Integer;
var
  Code: Integer;
begin
  if Exec(PowerShellExe(), '-NoProfile -ExecutionPolicy Bypass -File "' + Script + '" ' + Params + ' -Out "' +
          OutFile + '"', '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Result := Code
  else
    Result := -1;
end;

// The builds the link script finds, and what it can do with each (state|name|folder|profile|note per line).
procedure LoadPlan();
var
  Line: String;
  Lines: TStringList;
  I, Code: Integer;
begin
  if PlanLoaded then
    Exit;
  PlanLoaded := True;
  BState := TStringList.Create;
  BName := TStringList.Create;
  BPath := TStringList.Create;
  BProfile := TStringList.Create;
  BNote := TStringList.Create;
  try
    ExtractTemporaryFile(LinkScript);
  except
    Log('Resonite builds: could not extract the link script');
    Exit;
  end;
  Code := RunLinkScript(ExpandConstant('{tmp}\' + LinkScript), '-Plan', ExpandConstant('{tmp}\resonite_plan.txt'));
  Lines := ReadReport(ExpandConstant('{tmp}\resonite_plan.txt'));
  try
    for I := 0 to Lines.Count - 1 do
    begin
      Line := Lines.Strings[I];
      if Pos('|', Line) = 0 then
        Continue;
      Log('Resonite build: ' + Line);
      BState.Add(NextField(Line));
      BName.Add(NextField(Line));
      BPath.Add(NextField(Line));
      BProfile.Add(NextField(Line));
      BNote.Add(Line);
    end;
  finally
    Lines.Free;
  end;
  Log('Resonite builds found: ' + IntToStr(BState.Count) + ' (link script exit code ' + IntToStr(Code) + ')');
end;

// The Resonite tasks show when Steam's Resonite is installed or the link script finds another build.
function AnyResonite(): Boolean;
begin
  LoadPlan();
  Result := ResoniteFound() or (BState.Count > 0);
end;

// States in which a build loads, or can load, a Gale profile.
function IsGaleState(State: String): Boolean;
begin
  Result := (State = 'ready') or (State = 'linked') or (State = 'broken');
end;

// The builds that load, or can load, the Gale profile at Path, by name ('' for none).
function ProfileOfBuild(Path: String): String;
var
  I: Integer;
begin
  Result := '';
  if not PlanLoaded then
    Exit;
  for I := 0 to BState.Count - 1 do
    if IsGaleState(BState.Strings[I]) and (CompareText(BProfile.Strings[I], Path) = 0) then
    begin
      if Result <> '' then
        Result := Result + ', ';
      Result := Result + BName.Strings[I];
    end;
end;

// Finds the places BepInEx plugins can go: every Gale profile with BepisLoader, and the game folder if BepisLoader
// is installed there. The Gale profile Resonite started with last (newest BepInEx log) is the default.
procedure FindModTargets();
var
  F, LogFile: TFindRec;
  Profiles: String;
  NewestHigh, NewestLow: Longint;
  I: Integer;
begin
  if ModTargets <> nil then
    Exit;
  ModTargets := TStringList.Create;
  ModTargetNames := TStringList.Create;
  ModTargetIsGale := TStringList.Create;
  ModTargetDefault := -1;
  NewestHigh := 0;
  NewestLow := 0;
  Profiles := ExpandConstant('{userappdata}\com.kesomannen.gale\resonite\profiles');
  if FindFirst(Profiles + '\*', F) then
  try
    repeat
      if IsDirectory(F) and DirExists(Profiles + '\' + F.Name + '\BepInEx\core') then
      begin
        ModTargets.Add(Profiles + '\' + F.Name);
        ModTargetNames.Add('Gale profile "' + F.Name + '"');
        ModTargetIsGale.Add('1');
        if FindFirst(Profiles + '\' + F.Name + '\BepInEx\LogOutput.log', LogFile) then
        begin
          if (LogFile.LastWriteTime.dwHighDateTime > NewestHigh) or
             ((LogFile.LastWriteTime.dwHighDateTime = NewestHigh) and
              (LogFile.LastWriteTime.dwLowDateTime > NewestLow)) then
          begin
            NewestHigh := LogFile.LastWriteTime.dwHighDateTime;
            NewestLow := LogFile.LastWriteTime.dwLowDateTime;
            ModTargetDefault := ModTargets.Count - 1;
          end;
          FindClose(LogFile);
        end;
      end;
    until not FindNext(F);
  finally
    FindClose(F);
  end;
  if (ModTargetDefault < 0) and (ModTargets.Count > 0) then
    ModTargetDefault := 0;
  if ModTargetDefault >= 0 then
    ModTargetNames.Strings[ModTargetDefault] := ModTargetNames.Strings[ModTargetDefault] + ' (last used)';
  LoadPlan();
  for I := 0 to ModTargets.Count - 1 do
    if ProfileOfBuild(ModTargets.Strings[I]) <> '' then
      ModTargetNames.Strings[I] := ModTargetNames.Strings[I] + ' (used by ' + ProfileOfBuild(ModTargets.Strings[I]) + ')';
  // A linked game folder's BepInEx is its profile's: not a place of its own
  if ResoniteFound() and DirExists(GetResoniteDir() + '\BepInEx\core') and
     not FileExists(GetResoniteDir() + '\CyberFinger-GaleLink.json') then
  begin
    ModTargets.Add(GetResoniteDir());
    ModTargetNames.Add('The Resonite folder (BepInEx installed there, without a mod manager)');
    ModTargetIsGale.Add('0');
  end;
end;

function BepInExModSelected(): Boolean;
begin
  Result := WizardIsTaskSelected('resonite\morefluxactions') or WizardIsTaskSelected('resonite\steamvrrolefix');
end;

function RmlModSelected(): Boolean;
begin
  Result := WizardIsTaskSelected('resonite\cyberfingermod') or WizardIsTaskSelected('resonite\proximitygrab');
end;

// True for a target the user ticked on the Resonite plugins page.
function TargetChosen(I: Integer): Boolean;
begin
  Result := (TargetPage <> nil) and TargetPage.Values[I];
end;

// True when the plugins folder holds a plugin called Name: a folder or file with it in its name (Gale names
// the folders "<Team>-<Name>"). One disabled in Gale doesn't count: Gale renames its files *.old.
function HasPlugin(Plugins, Name: String): Boolean;
var
  F: TFindRec;
begin
  Result := False;
  if FindFirst(Plugins + '\*', F) then
  try
    repeat
      Result := (Pos(Lowercase(Name), Lowercase(F.Name)) > 0) and (CompareText(ExtractFileExt(F.Name), '.old') <> 0) and
                not FileExists(Plugins + '\' + F.Name + '\manifest.json.old');
    until Result or not FindNext(F);
  finally
    FindClose(F);
  end;
end;

// True when a Steam user's localconfig.vdf gives Resonite launch options that load RML: in a section of its app ID,
// a "LaunchOptions" with -LoadAssembly and ResoniteModLoader after it (RML's own setup).
function LaunchOptionsLoadRml(FileName: String): Boolean;
var
  Lines: TArrayOfString;
  Line: String;
  I, Depth, P: Integer;
begin
  Result := False;
  if not LoadStringsFromFile(FileName, Lines) then
    Exit;
  Depth := 0;                             // in a "2519830" section: how deep, -1 before its opening brace
  for I := 0 to GetArrayLength(Lines) - 1 do
  begin
    Line := Lowercase(Trim(Lines[I]));
    if Depth < 0 then
    begin
      if Line = '{' then
        Depth := 1
      else
        Depth := 0;
    end
    else if Depth = 0 then
    begin
      if Line = '"2519830"' then
        Depth := -1;
    end
    else if Line = '{' then
      Depth := Depth + 1
    else if Line = '}' then
      Depth := Depth - 1
    else if (Depth = 1) and (Pos('"launchoptions"', Line) = 1) then
    begin
      P := Pos('-loadassembly', Line);
      if (P > 0) and (Pos('resonitemodloader', Copy(Line, P, Length(Line))) > 0) then
      begin
        Result := True;
        Exit;
      end;
    end;
  end;
end;

// True when Resonite's Steam launch options load RML, for the Steam user logged in, else for any user of this PC.
function SteamLoadsRml(): Boolean;
var
  UserData: String;
  User: Cardinal;
  F: TFindRec;
begin
  Result := False;
  if not RegQueryStringValue(HKCU, 'Software\Valve\Steam', 'SteamPath', UserData) then
    Exit;
  StringChangeEx(UserData, '/', '\', True);
  UserData := UserData + '\userdata';
  if RegQueryDWordValue(HKCU, 'Software\Valve\Steam\ActiveProcess', 'ActiveUser', User) and (User <> 0) and
     DirExists(UserData + '\' + IntToStr(User)) then
  begin
    Result := LaunchOptionsLoadRml(UserData + '\' + IntToStr(User) + '\config\localconfig.vdf');
    Exit;
  end;
  if FindFirst(UserData + '\*', F) then
  try
    repeat
      if IsDirectory(F) then
        Result := LaunchOptionsLoadRml(UserData + '\' + F.Name + '\config\localconfig.vdf');
    until Result or not FindNext(F);
  finally
    FindClose(F);
  end;
end;

// True when a build ticked on the builds page to load its Gale profile is at Path, or (IsProfile) loads the profile
// at Path. How that build starts ResoniteModLoader is then for the link script to check, with the build's shortcuts
// and launch options: its report says what to change.
function LinkedOnBuildsPage(Path: String; IsProfile: Boolean): Boolean;
var
  I, B: Integer;
  Value: String;
begin
  Result := False;
  if BuildsPage = nil then
    Exit;
  for I := 0 to BuildItem.Count - 1 do
  begin
    B := StrToInt(BuildItem.Strings[I]);
    if IsProfile then
      Value := BProfile.Strings[B]
    else
      Value := BPath.Strings[B];
    if BuildsPage.Values[I] and IsGaleState(BState.Strings[B]) and (CompareText(Value, Path) = 0) then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

// In the supported setup, ResoniteModLoaderLoader in the Gale profile loads RML, and nothing passes -LoadAssembly: with
// both, RML loads twice and Resonite stops as it starts ("An item with the same key has already been added. Key:
// ResoniteModLoader"), its renderer window left waiting. Returns a note about Resonite's Steam launch options, or ''
// (also when Steam's Resonite is linked on the builds page: the link script's report says it then).
function SteamRmlNote(): String;
begin
  Result := '';
  if SteamLoadsRml() and not LinkedOnBuildsPage(GetResoniteDir(), False) then
    Result := #13#10#13#10 + 'Resonite''s Steam launch options load ResoniteModLoader themselves (-LoadAssembly ' +
              'Libraries/ResoniteModLoader.dll). Remove that from them (Steam: Resonite > Properties > General > Launch ' +
              'options), and let ResoniteModLoaderLoader in your Gale profile load RML: with both, RML loads twice and ' +
              'Resonite stops as it starts.';
end;

procedure AddMissing(var Missing: String; Needed: Boolean; Target, Name: String);
begin
  if Needed and not HasPlugin(Target + '\BepInEx\plugins', Name) then
    Missing := Missing + ', ' + Name;
end;

// The packages the chosen mods need that target I lacks, as a line, or ''.
function MissingPackages(I: Integer): String;
var
  Target, Missing: String;
  Flux, RoleFix: Boolean;
begin
  Target := ModTargets.Strings[I];
  Flux := WizardIsTaskSelected('resonite\morefluxactions');
  RoleFix := WizardIsTaskSelected('resonite\steamvrrolefix');
  Missing := '';
  AddMissing(Missing, Flux, Target, 'BepisResoniteWrapper');
  AddMissing(Missing, Flux, Target, 'BepInExResoniteShim');
  AddMissing(Missing, Flux, Target, 'InterprocessLib');
  AddMissing(Missing, Flux, Target, 'RenderiteHook');
  if (Flux or RoleFix) and not DirExists(Target + '\Renderer\BepInEx\core') then
    Missing := Missing + ', BepInExRenderer';
  // RML mods need ResoniteModLoaderLoader in the profile (the supported setup: no -LoadAssembly anywhere). A profile
  // that a build linked on the builds page loads is the link script's to check, with that build's shortcuts.
  AddMissing(Missing, RmlModSelected() and (ModTargetIsGale.Strings[I] = '1') and not LinkedOnBuildsPage(Target, True),
             Target, 'ResoniteModLoaderLoader');
  Result := '';
  if Missing <> '' then
    Result := #13#10 + '- ' + ModTargetNames.Strings[I] + ': ' + Copy(Missing, 3, Length(Missing));
end;

// Copies the folder Src into Dst, creating it and replacing files.
function CopyTree(Src, Dst: String): Boolean;
var
  F: TFindRec;
begin
  Result := ForceDirectories(Dst);
  if Result and FindFirst(Src + '\*', F) then
  try
    repeat
      if IsDirectory(F) then
        Result := CopyTree(Src + '\' + F.Name, Dst + '\' + F.Name) and Result
      else if (F.Attributes and FILE_ATTRIBUTE_DIRECTORY) = 0 then
        Result := CopyFile(Src + '\' + F.Name, Dst + '\' + F.Name, False) and Result;
    until not FindNext(F);
  finally
    FindClose(F);
  end;
end;

procedure Remember(Installed: TStringList; Path: String);
begin
  if Installed.IndexOf(Path) < 0 then
    Installed.Add(Path);
end;

// Before a copy replaces Dest: when Dest is the user's own (not a copy of an earlier setup), it is kept as a backup
// that the uninstaller puts back. Plugin folders go beside the plugins folder (BepInEx would load a backup inside
// it), files get a .pre-cyberfinger suffix (ResoniteModLoader loads *.dll only).
procedure KeepOriginal(Dest: String; IsDir: Boolean; Installed, Backups: TStringList);
var
  Backup: String;
begin
  if Installed.IndexOf(Dest) >= 0 then
    Exit;
  if IsDir then
  begin
    if not DirExists(Dest) then
      Exit;
    Backup := ExtractFileDir(Dest) + '.pre-cyberfinger\' + ExtractFileName(Dest);
  end
  else
  begin
    if not FileExists(Dest) then
      Exit;
    Backup := Dest + '.pre-cyberfinger';
  end;
  if DirExists(Backup) or FileExists(Backup) then
    Exit;
  if ForceDirectories(ExtractFileDir(Backup)) and RenameFile(Dest, Backup) then
  begin
    Backups.Add(Dest + '|' + Backup);
    Log('Kept ' + Dest + ' as ' + Backup);
  end
  else
    Log('Could not keep ' + Dest + ' as ' + Backup);
end;

// Replaces the plugin folder Rel (e.g. Renderer\BepInEx\plugins\DrSciCortex-SteamVRRoleFix) in Target with ours.
procedure InstallPlugin(Rel, Target: String; Installed, Backups: TStringList; var Failed: String);
var
  Dest: String;
begin
  Dest := Target + '\' + Rel;
  KeepOriginal(Dest, True, Installed, Backups);
  if DirExists(Dest) then
    DelTree(Dest, True, True, True);
  if CopyTree(ExpandConstant('{app}\ResoniteMods\') + Rel, Dest) then
    Remember(Installed, Dest)
  else
    Failed := Failed + #13#10 + Dest;
end;

procedure InstallRmlMod(Name, GameDir: String; Installed, Backups: TStringList; var Failed: String);
var
  Dest: String;
begin
  Dest := GameDir + '\rml_mods\' + Name;
  KeepOriginal(Dest, False, Installed, Backups);
  if ForceDirectories(ExtractFileDir(Dest)) and
     CopyFile(ExpandConstant('{app}\ResoniteMods\rml_mods\') + Name, Dest, False) then
    Remember(Installed, Dest)
  else
    Failed := Failed + #13#10 + Dest;
end;

// Without a config of its own, CyberFingerMod starts with GamepadBindings off: with this driver CyberFinger is a
// SteamVR controller, not a gamepad. An existing config is left alone. Returns a note for the user, or ''.
function SetUpCyberFingerModConfig(GameDir: String; Installed: TStringList): String;
var
  FileName: String;
  Raw: AnsiString;
begin
  Result := '';
  FileName := GameDir + '\rml_config\CyberFingerMod.json';
  if FileExists(FileName) then
  begin
    if (Installed.IndexOf(FileName) < 0) and LoadStringFromFile(FileName, Raw) and
       (Pos('"GamepadBindings": true', Raw) > 0) then
      Result := #13#10#13#10 + 'CyberFingerMod''s GamepadBindings setting is on. With the SteamVR driver, turn it off ' +
                '(in Resonite''s mod settings, or in ' + FileName + ').';
    Exit;
  end;
  if ForceDirectories(ExtractFileDir(FileName)) and
     SaveStringToFile(FileName, '{' + #13#10 + '  "version": "1.0.0",' + #13#10 + '  "values": {' + #13#10 +
                      '    "GamepadBindings": false' + #13#10 + '  }' + #13#10 + '}' + #13#10, False) then
    Remember(Installed, FileName);
end;

// False for a build the user unticked on the "Resonite builds" page: it stays as it is, without our mods too. A
// build the page doesn't list (without the link script's plan, Steam's Resonite) is True.
function BuildChosen(Path: String): Boolean;
var
  I: Integer;
begin
  Result := True;
  if BuildsPage = nil then
    Exit;
  for I := 0 to BuildItem.Count - 1 do
    if CompareText(BPath.Strings[StrToInt(BuildItem.Strings[I])], Path) = 0 then
    begin
      Result := BuildsPage.Values[I];
      Exit;
    end;
end;

// The game folders that get the ResoniteModLoader mods: Steam's Resonite, and the other builds found that have
// ResoniteModLoader, unless unticked on the "Resonite builds" page.
function RmlGameDirs(): TStringList;
var
  I: Integer;
begin
  Result := TStringList.Create;
  LoadPlan();
  if ResoniteFound() and BuildChosen(GetResoniteDir()) then
    Result.Add(GetResoniteDir());
  for I := 0 to BPath.Count - 1 do
    if (Result.IndexOf(BPath.Strings[I]) < 0) and (CompareText(BPath.Strings[I], GetResoniteDir()) <> 0) and
       FileExists(BPath.Strings[I] + '\Libraries\ResoniteModLoader.dll') and BuildChosen(BPath.Strings[I]) then
      Result.Add(BPath.Strings[I]);
end;

procedure InstallResoniteMods();
var
  ListFile, BackupFile, Target, Failed, Notes, Missing: String;
  Raw: AnsiString;
  Installed, Backups, GameDirs: TStringList;
  I, Chosen: Integer;
begin
  ListFile := ExpandConstant('{app}\ResoniteMods\') + ResoniteModsList;
  BackupFile := ExpandConstant('{app}\ResoniteMods\') + ResoniteBackupList;
  Failed := '';
  Notes := '';
  Missing := '';
  Chosen := 0;
  Installed := TStringList.Create;
  Backups := TStringList.Create;
  try
    // Copies from an earlier setup stay listed, so that the uninstaller removes those too, and what they replaced
    if LoadStringFromFile(ListFile, Raw) then
      Installed.Text := Utf8Decode(Raw);
    if LoadStringFromFile(BackupFile, Raw) then
      Backups.Text := Utf8Decode(Raw);

    FindModTargets();
    if BepInExModSelected() then
    begin
      for I := 0 to ModTargets.Count - 1 do
      begin
        Target := ModTargets.Strings[I];
        // The game folder, just linked on the builds page: its BepInEx is now the profile's, a target of its own
        if not TargetChosen(I) or
           ((ModTargetIsGale.Strings[I] = '0') and FileExists(Target + '\CyberFinger-GaleLink.json')) then
          Continue;
        Chosen := Chosen + 1;
        if WizardIsTaskSelected('resonite\morefluxactions') then
        begin
          InstallPlugin('BepInEx\plugins\DrSciCortex-MoreFluxActions', Target, Installed, Backups, Failed);
          if DirExists(Target + '\Renderer\BepInEx\core') then
            InstallPlugin('Renderer\BepInEx\plugins\DrSciCortex-MoreFluxActions', Target, Installed, Backups, Failed);
        end;
        if WizardIsTaskSelected('resonite\steamvrrolefix') and DirExists(Target + '\Renderer\BepInEx\core') then
          InstallPlugin('Renderer\BepInEx\plugins\DrSciCortex-SteamVRRoleFix', Target, Installed, Backups, Failed);
        Missing := Missing + MissingPackages(I);
      end;
      if ModTargets.Count = 0 then
        Notes := 'No BepisLoader was found, neither in a Gale profile nor in the Resonite folder, so MoreFluxActions ' +
                 'and SteamVRRoleFix were not installed. Set up Gale with BepisLoader and BepInExRenderer ' +
                 '(https://modding.resonite.net/getting-started/installation/), then run this setup again. They are ' +
                 'also in ' + ExpandConstant('{app}\ResoniteMods') + ', laid out as they install, to copy by hand.'
      else if Chosen = 0 then
        Notes := 'No Gale profile or Resonite folder was chosen, so MoreFluxActions and SteamVRRoleFix were not ' +
                 'installed. They are in ' + ExpandConstant('{app}\ResoniteMods') + ', laid out as they install.';
    end;
    if Missing <> '' then
      Notes := Notes + #13#10#13#10 + 'The mods need these packages, which aren''t installed or are disabled. Add ' +
               'them in Gale (or your mod manager), then start Resonite:' + Missing;

    GameDirs := RmlGameDirs();
    try
      for I := 0 to GameDirs.Count - 1 do
      begin
        if WizardIsTaskSelected('resonite\cyberfingermod') then
        begin
          InstallRmlMod('CyberFingerMod.dll', GameDirs.Strings[I], Installed, Backups, Failed);
          Notes := Notes + SetUpCyberFingerModConfig(GameDirs.Strings[I], Installed);
        end;
        if WizardIsTaskSelected('resonite\proximitygrab') then
          InstallRmlMod('ProximityGrab.dll', GameDirs.Strings[I], Installed, Backups, Failed);
      end;
    finally
      GameDirs.Free;
    end;
    if RmlModSelected() and ResoniteFound() and BuildChosen(GetResoniteDir()) and
       not FileExists(GetResoniteDir() + '\Libraries\ResoniteModLoader.dll') then
      Notes := Notes + #13#10#13#10 + 'CyberFingerMod and ProximityGrab are ResoniteModLoader mods: they load once ' +
               'ResoniteModLoader is installed (https://github.com/resonite-modding-group/ResoniteModLoader).';
    Notes := Notes + SteamRmlNote();

    SaveStringToFile(ListFile, Utf8Encode(Installed.Text), False);
    SaveStringToFile(BackupFile, Utf8Encode(Backups.Text), False);
  finally
    Installed.Free;
    Backups.Free;
  end;
  if Failed <> '' then
    SuppressibleMsgBox('Some Resonite mod files could not be copied (is Resonite running?):' + Failed, mbError, MB_OK, IDOK);
  if Notes <> '' then
    SuppressibleMsgBox(Trim(Notes), mbInformation, MB_OK, IDOK);
end;

// Removes the copies InstallResoniteMods made (our plugin folders, the RML mods, a CyberFingerMod config it
// created), then puts back what they replaced.
procedure UninstallResoniteMods();
var
  Raw: AnsiString;
  Paths: TStringList;
  Path, Backup: String;
  I, P: Integer;
begin
  Paths := TStringList.Create;
  try
    if LoadStringFromFile(ExpandConstant('{app}\ResoniteMods\') + ResoniteModsList, Raw) then
    begin
      Paths.Text := Utf8Decode(Raw);
      for I := 0 to Paths.Count - 1 do
      begin
        Path := Trim(Paths.Strings[I]);
        if Pos('DrSciCortex-', ExtractFileName(Path)) = 1 then
          DelTree(Path, True, True, True)
        else if CompareText(ExtractFileName(ExtractFileDir(Path)), 'rml_mods') = 0 then
          DeleteFile(Path)
        else if (CompareText(ExtractFileName(Path), 'CyberFingerMod.json') = 0) and
                (CompareText(ExtractFileName(ExtractFileDir(Path)), 'rml_config') = 0) then
          DeleteFile(Path);
      end;
    end;
    if LoadStringFromFile(ExpandConstant('{app}\ResoniteMods\') + ResoniteBackupList, Raw) then
    begin
      Paths.Text := Utf8Decode(Raw);
      for I := 0 to Paths.Count - 1 do
      begin
        P := Pos('|', Paths.Strings[I]);
        if P = 0 then
          Continue;
        Path := Copy(Paths.Strings[I], 1, P - 1);
        Backup := Copy(Paths.Strings[I], P + 1, Length(Paths.Strings[I]));
        if not FileExists(Path) and not DirExists(Path) and (FileExists(Backup) or DirExists(Backup)) then
        begin
          if RenameFile(Backup, Path) then
            Log('Restored ' + Path)
          else
            Log('Could not restore ' + Path + ' from ' + Backup);
          if Pos('.pre-cyberfinger\', Backup) > 0 then
            RemoveDir(ExtractFileDir(Backup));      // the plugins.pre-cyberfinger folder, once empty
        end;
      end;
    end;
  finally
    Paths.Free;
  end;
end;

// Undoes every link the link script made on this PC (setup's, and those made from the Start menu): each build is
// restored from the record of the changes it keeps.
// Returns False to cancel the uninstall, when a build could not be restored and the user would rather fix that first
// (the script goes with the uninstall).
function UnlinkResoniteBuilds(): Boolean;
var
  Script, OutFile, Report: String;
  Lines: TStringList;
  Code, I: Integer;
begin
  Result := True;
  Script := ExpandConstant('{app}\tools\' + LinkScript);
  if not FileExists(Script) then
    Exit;
  OutFile := ExpandConstant('{tmp}\resonite_unlink.txt');
  Code := RunLinkScript(Script, '-Unlink -All -Yes', OutFile);
  Log('Resonite builds unlinked (link script exit code ' + IntToStr(Code) + ')');
  if Code = 0 then
    Exit;
  Report := '';
  Lines := ReadReport(OutFile);
  try
    for I := 0 to Lines.Count - 1 do
      Report := Report + #13#10 + Trim(Lines.Strings[I]);
  finally
    Lines.Free;
  end;
  Result := SuppressibleMsgBox('Some Resonite builds could not be restored to how they were before CyberFinger setup:' +
                               Report + #13#10#13#10 + 'Uninstall anyway? They then keep loading their Gale profile; ' +
                               'the README ("Loading Gale mods without Gale''s launcher") shows how to undo that by ' +
                               'hand.' + #13#10#13#10 + 'Choose No to cancel, close what holds them (Resonite, Gale, an ' +
                               'Explorer window), and uninstall again.', mbConfirmation, MB_YESNO, IDYES) = IDYES;
end;

// Links the builds ticked on the "Resonite builds" page to their Gale profiles, or without Gale installs
// SteamVRRoleFix into them, then shows what the link script reported.
procedure LinkResoniteBuilds();
var
  OutFile, Params, Report: String;
  Lines: TStringList;
  I, B, Code: Integer;
begin
  if BuildsPage = nil then
    Exit;
  OutFile := ExpandConstant('{tmp}\resonite_links.txt');
  for I := 0 to BuildItem.Count - 1 do
  begin
    if not BuildsPage.Values[I] then
      Continue;
    B := StrToInt(BuildItem.Strings[I]);
    if IsGaleState(BState.Strings[B]) then
      Params := '-Game "' + BPath.Strings[B] + '" -GaleProfile "' + BProfile.Strings[B] + '"'
    else if WizardIsTaskSelected('resonite\steamvrrolefix') then
      Params := '-Game "' + BPath.Strings[B] + '" -Standalone -Source "' + ExpandConstant('{app}\ResoniteMods') + '"'
    else
      Continue;
    Code := RunLinkScript(ExpandConstant('{app}\tools\' + LinkScript), Params + ' -Yes', OutFile);
    Log('Resonite build ' + BName.Strings[B] + ': link script exit code ' + IntToStr(Code));
  end;
  Report := '';
  Lines := ReadReport(OutFile);
  try
    for I := 0 to Lines.Count - 1 do
      Report := Report + #13#10 + Lines.Strings[I];
  finally
    Lines.Free;
  end;
  if Report <> '' then
    SuppressibleMsgBox('Resonite builds:' + #13#10 + Report + #13#10#13#10 + 'The README ("Loading Gale mods without ' +
                       'Gale''s launcher") explains this, and how to switch it off.', mbInformation, MB_OK, IDOK);
end;

function IsResoniteRunning(): Boolean;
begin
  Result := IsProcessRunning('Resonite.exe') or IsProcessRunning('Renderite.Host.exe') or
            IsProcessRunning('Renderite.Renderer.exe');
end;

// Resonite holds its mods' files while running.
function WaitForResoniteClosed(): Boolean;
begin
  Result := True;
  while IsResoniteRunning() do
  begin
    if SuppressibleMsgBox('Resonite is running. Please quit Resonite, then click Retry.',
                          mbError, MB_RETRYCANCEL, IDCANCEL) = IDCANCEL then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

// ── Installs under the old AppId ───────────────────────────────────────────
// Up to 2.2.5 the AppId was a template placeholder, which other programs may share. Setup doesn't see such an
// install as its own (it would add a second one beside it), so it removes it first: only an entry named
// CyberFinger Bridge, and only once the user has clicked Install (PrepareToInstall). Its folder is kept as the
// default install folder.
const
  LegacyAppKey = 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}_is1';

function FindLegacyInstall(var Uninstaller, Location: String): Boolean;
var
  Roots: array[0..2] of Integer;
  I: Integer;
  Name: String;
begin
  Result := False;
  Roots[0] := HKLM32;
  Roots[1] := HKLM64;
  Roots[2] := HKCU;
  for I := 0 to 2 do
  begin
    if ((Roots[I] = HKLM64) and not IsWin64) or not RegQueryStringValue(Roots[I], LegacyAppKey, 'DisplayName', Name) then
      Continue;
    if Pos('{#MyAppName}', Name) <> 1 then
    begin
      Log('Old AppId entry belongs to another program: ' + Name);
      Continue;
    end;
    if not RegQueryStringValue(Roots[I], LegacyAppKey, 'UninstallString', Uninstaller) then
      Continue;
    Uninstaller := RemoveQuotes(Uninstaller);
    if not RegQueryStringValue(Roots[I], LegacyAppKey, 'InstallLocation', Location) then
      Location := '';
    Result := True;
    Exit;
  end;
end;

function RemoveLegacyInstall(): String;
var
  Uninstaller, Location: String;
  Code, Tries: Integer;
begin
  Result := '';
  if not FindLegacyInstall(Uninstaller, Location) then
    Exit;
  // Its uninstaller undoes the Resonite links and mod copies: Resonite must be closed for that.
  if not WaitForResoniteClosed() then
  begin
    Result := 'Resonite must be closed to replace the earlier CyberFinger Bridge.';
    Exit;
  end;
  Log('Removing the earlier CyberFinger Bridge: ' + Uninstaller);
  if not FileExists(Uninstaller) or
     not Exec(Uninstaller, '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Code := -1;
  // The uninstaller finishes from a copy of itself: wait for its entry to go.
  Tries := 0;
  while FindLegacyInstall(Uninstaller, Location) and (Tries < 60) do
  begin
    Sleep(500);
    Tries := Tries + 1;
  end;
  if FindLegacyInstall(Uninstaller, Location) then
    Result := 'The earlier CyberFinger Bridge could not be removed (exit code ' + IntToStr(Code) + '). Please ' +
              'uninstall it from Windows Settings > Apps, then run this setup again.'
  else
    Log('Earlier CyberFinger Bridge removed');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := RemoveLegacyInstall();
end;

function InitializeSetup(): Boolean;
begin
  Result := WaitForSteamVRClosed();
end;

function InitializeUninstall(): Boolean;
begin
  Result := WaitForSteamVRClosed();
  if Result and (FileExists(ExpandConstant('{app}\ResoniteMods\') + ResoniteModsList) or
                 FileExists(ExpandConstant('{localappdata}\CyberFinger\gale_links.json'))) then
    Result := WaitForResoniteClosed();
end;

// Back to how things were before CyberFinger setup: the builds' links undone, our mod copies removed, and what they
// replaced put back.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    if not UnlinkResoniteBuilds() then
      Abort;                              // before anything is removed: uninstalling again does it all
    UninstallResoniteMods();
  end;
end;

#if HaveResoniteMods
// The "Resonite builds" page: per build found, load its Gale profile whenever it starts, or without Gale install
// SteamVRRoleFix into it. Returns the page's ID, or AfterID when there are no builds.
function CreateBuildsPage(AfterID: Integer): Integer;
var
  I: Integer;
  Advice, Caption: String;
  HaveGale: Boolean;
begin
  Result := AfterID;
  LoadPlan();
  if BState.Count = 0 then
    Exit;
  // What the builds without a Gale profile to load need for one. It goes above the list: the page places the list
  // below its text once, when created.
  Advice := '';
  HaveGale := True;
  for I := 0 to BState.Count - 1 do
    if BState.Strings[I] = 'nogale' then
      HaveGale := False
    else if not IsGaleState(BState.Strings[I]) and (BState.Strings[I] <> 'standalone') then
      Advice := Advice + #13#10 + '- ' + BName.Strings[I] + ': ' + BNote.Strings[I];
  if not HaveGale then
    Advice := #13#10#13#10 + 'Recommended: Gale (https://github.com/Kesomannen/gale), which MoreFluxActions needs. ' +
              'Install it, make a profile per build named like the build, then run this setup again.'
  else if Advice <> '' then
    Advice := #13#10#13#10 + 'To load Gale mods in these too, then run this setup again:' + Advice;
  BuildItem := TStringList.Create;
  BuildsPage := CreateInputOptionPage(AfterID, 'Resonite builds',
    'Which builds should get the CyberFinger mods?',
    'Ticked builds get CyberFingerMod and ProximityGrab, and load their Gale profile''s mods however they start ' +
    '(Steam, SteamVR, a shortcut), without Gale''s Launch button or a launch script. Builds without a Gale profile get ' +
    'SteamVRRoleFix on its own. Unticked builds stay as they are: nothing is installed in them. The uninstaller ' +
    'undoes it all; the README ("Loading Gale mods without Gale''s launcher") shows how to switch it off.' + Advice,
    False, False);
  for I := 0 to BState.Count - 1 do
  begin
    if IsGaleState(BState.Strings[I]) then
      Caption := BName.Strings[I] + ': load Gale profile "' + ExtractFileName(BProfile.Strings[I]) + '"'
    else if BState.Strings[I] = 'standalone' then
      Caption := BName.Strings[I] + ': update SteamVRRoleFix, installed without Gale'
    else
      Caption := BName.Strings[I] + ': install SteamVRRoleFix without Gale';
    BuildsPage.Add(Caption + '   ' + BPath.Strings[I]);
    BuildsPage.Values[BuildItem.Count] := True;
    BuildItem.Add(IntToStr(I));
  end;
  Result := BuildsPage.ID;
end;

// True when the build item I on the builds page is ticked for linking to a Gale profile.
function LinkTicked(I: Integer): Boolean;
begin
  Result := (BuildsPage <> nil) and BuildsPage.Values[I] and IsGaleState(BState.Strings[StrToInt(BuildItem.Strings[I])]);
end;

procedure InitializeWizard();
var
  I, After: Integer;
  RmlWhere, LegacyUninstaller, LegacyLocation: String;
begin
  // An install under the old AppId: install into its folder (it is removed first, in PrepareToInstall).
  if FindLegacyInstall(LegacyUninstaller, LegacyLocation) and (LegacyLocation <> '') then
    WizardForm.DirEdit.Text := RemoveBackslashUnlessRoot(LegacyLocation);
  After := wpSelectTasks;
  if AnyResonite() then
    After := CreateBuildsPage(wpSelectTasks);
  FindModTargets();
  if ModTargets.Count = 0 then
    Exit;
  if BuildsPage <> nil then
    RmlWhere := 'each build ticked on the Resonite builds page, if it has ResoniteModLoader'
  else
    RmlWhere := 'Resonite''s folder';
  TargetPage := CreateInputOptionPage(After, 'Resonite plugins',
    'Where should MoreFluxActions and SteamVRRoleFix go?',
    'These are BepInEx plugins, and load from the BepInEx that Resonite starts with. Gale gives each profile its own: ' +
    'tick the profiles you play with (the ones your builds load are ticked). Without a mod manager, BepInEx is ' +
    'installed in the Resonite folder itself.' + #13#10#13#10 +
    'CyberFingerMod and ProximityGrab go into the rml_mods folder of ' + RmlWhere + '.',
    False, False);
  for I := 0 to ModTargets.Count - 1 do
  begin
    TargetPage.Add(ModTargetNames.Strings[I] + '   ' + ModTargets.Strings[I]);
    TargetPage.Values[I] := (I = ModTargetDefault) or (ModTargetIsGale.Strings[I] = '0') or
                            (ProfileOfBuild(ModTargets.Strings[I]) <> '');
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if (TargetPage <> nil) and (PageID = TargetPage.ID) then
    Result := not BepInExModSelected();
  if (BuildsPage <> nil) and (PageID = BuildsPage.ID) then
    Result := not WizardIsTaskSelected('resonite');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpReady) and WizardIsTaskSelected('resonite') then
    Result := WaitForResoniteClosed();
end;

procedure AddMemoPart(var Memo: String; Part, NewLine: String);
begin
  if Part = '' then
    Exit;
  if Memo <> '' then
    Memo := Memo + NewLine + NewLine;
  Memo := Memo + Part;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo, MemoComponentsInfo,
                         MemoGroupInfo, MemoTasksInfo: String): String;
var
  Part, Where: String;
  I, B: Integer;
  Dirs: TStringList;
begin
  Result := '';
  AddMemoPart(Result, MemoUserInfoInfo, NewLine);
  AddMemoPart(Result, MemoDirInfo, NewLine);
  AddMemoPart(Result, MemoTypeInfo, NewLine);
  AddMemoPart(Result, MemoComponentsInfo, NewLine);
  AddMemoPart(Result, MemoGroupInfo, NewLine);
  AddMemoPart(Result, MemoTasksInfo, NewLine);
  if WizardIsTaskSelected('resonite') then
  begin
    FindModTargets();
    Part := 'Resonite mods go into:';
    if BepInExModSelected() then
    begin
      Where := '';
      for I := 0 to ModTargets.Count - 1 do
        if TargetChosen(I) then
          Where := Where + NewLine + Space + Space + ModTargets.Strings[I];
      if ModTargets.Count = 0 then
        Where := NewLine + Space + Space + 'skipped: no BepisLoader found, in a Gale profile or the Resonite folder'
      else if Where = '' then
        Where := NewLine + Space + Space + 'nowhere: no Gale profile or Resonite folder chosen';
      Part := Part + NewLine + Space + 'MoreFluxActions, SteamVRRoleFix (BepInEx):' + Where;
    end;
    if RmlModSelected() then
    begin
      Part := Part + NewLine + Space + 'CyberFingerMod, ProximityGrab (ResoniteModLoader):';
      Dirs := RmlGameDirs();
      try
        for I := 0 to Dirs.Count - 1 do
          Part := Part + NewLine + Space + Space + Dirs.Strings[I] + '\rml_mods';
        if Dirs.Count = 0 then
          Part := Part + NewLine + Space + Space + 'nowhere: no build chosen on the Resonite builds page';
      finally
        Dirs.Free;
      end;
    end;
    AddMemoPart(Result, Part, NewLine);
    if BuildsPage <> nil then
    begin
      Part := '';
      for I := 0 to BuildItem.Count - 1 do
      begin
        B := StrToInt(BuildItem.Strings[I]);
        if LinkTicked(I) then
          Part := Part + NewLine + Space + BName.Strings[B] + ': loads Gale profile "' +
                  ExtractFileName(BProfile.Strings[B]) + '" however it starts'
        else if BuildsPage.Values[I] and WizardIsTaskSelected('resonite\steamvrrolefix') then
          Part := Part + NewLine + Space + BName.Strings[B] + ': SteamVRRoleFix without Gale';
      end;
      if Part <> '' then
        AddMemoPart(Result, 'Resonite builds:' + Part, NewLine);
    end;
  end;
end;
#endif

// ── Leftovers of the proof-of-concept driver ───────────────────────────────
// Its README had users copy the driver into SteamVR\drivers\cyberfinger and add TrackingOverrides for
// "/devices/cyberfinger/..." to steamvr.vrsettings. That copy would clash with this driver, and the
// overrides still match its devices: whichever device holds a hand role would get the CyberFinger pose,
// so the hands would vanish whenever the driver is switched off.

const
  OldOverride = '"/devices/cyberfinger/';

procedure RemoveDriverInsideSteamVR();
var
  SteamVR, Old: String;
begin
  SteamVR := FindSteamVR();
  if SteamVR = '' then
    Exit;
  Old := SteamVR + '\drivers\cyberfinger';
  if not DirExists(Old) then
    Exit;
  if SuppressibleMsgBox('An older CyberFinger driver is installed inside SteamVR:' + #13#10 + Old + #13#10 + #13#10 +
                        'Remove it? It would conflict with the driver being installed.',
                        mbConfirmation, MB_YESNO, IDYES) = IDNO then
    Exit;
  if DelTree(Old, True, True, True) then
    Log('Removed the old driver folder ' + Old)
  else
    SuppressibleMsgBox('Could not remove ' + Old + '. Please delete that folder by hand.', mbError, MB_OK, IDOK);
end;

// The user's steamvr.vrsettings, or ''.
function FindSteamVRSettings(): String;
var
  SteamPath: String;
begin
  Result := ReadOpenVRPath('config');
  if (Result <> '') and FileExists(Result + '\steamvr.vrsettings') then
  begin
    Result := Result + '\steamvr.vrsettings';
    Exit;
  end;
  Result := '';
  if RegQueryStringValue(HKCU, 'Software\Valve\Steam', 'SteamPath', SteamPath) then
  begin
    StringChangeEx(SteamPath, '/', '\', True);
    if FileExists(SteamPath + '\config\steamvr.vrsettings') then
      Result := SteamPath + '\config\steamvr.vrsettings';
  end;
end;

// True for a trimmed line holding just one  "/devices/cyberfinger/<serial>" : "<path>"  entry.
function IsOldOverrideLine(Line: String): Boolean;
var
  T: String;
  P: Integer;
begin
  Result := False;
  T := Line;
  if Pos(OldOverride, T) <> 1 then
    Exit;
  if T[Length(T)] = ',' then
    T := TrimRight(Copy(T, 1, Length(T) - 1));
  P := Pos('"', Copy(T, 2, Length(T)));        // closing quote of the key, at P + 1
  if P = 0 then
    Exit;
  T := TrimLeft(Copy(T, P + 2, Length(T)));    // : "<path>"
  if Copy(T, 1, 1) <> ':' then
    Exit;
  T := TrimLeft(Copy(T, 2, Length(T)));        // "<path>"
  Result := (Length(T) >= 2) and (T[1] = '"') and (Pos('"', Copy(T, 2, Length(T))) = Length(T) - 1);
end;

// Drops those entries from the TrackingOverrides section of a steamvr.vrsettings text, line by line so
// that everything else stays as it was. Returns how many; Unhandled counts entries laid out otherwise.
function StripOldOverrides(var Text: String; var Unhandled: Integer): Integer;
var
  Rest, Line, T, Output: String;
  P, K: Integer;
  InSection, JustRemoved: Boolean;
begin
  Result := 0;
  Unhandled := 0;
  Output := '';
  Rest := Text;
  InSection := False;
  JustRemoved := False;
  while Rest <> '' do
  begin
    P := Pos(#10, Rest);
    if P = 0 then
      P := Length(Rest);
    Line := Copy(Rest, 1, P);
    Delete(Rest, 1, P);
    T := Trim(Line);
    if Pos('"TrackingOverrides"', T) > 0 then
    begin
      InSection := Pos('}', T) = 0;
      if Pos(OldOverride, T) > 0 then
        Unhandled := Unhandled + 1;
    end
    else if InSection and IsOldOverrideLine(T) then
    begin
      Result := Result + 1;
      JustRemoved := True;
      Continue;
    end
    else if InSection and (Pos(OldOverride, T) > 0) then
      Unhandled := Unhandled + 1;
    if T <> '' then
    begin
      if JustRemoved and (T[1] = '}') then
      begin
        // The section's last entry went: drop the comma now left before the closing brace.
        K := Length(Output);
        while (K > 0) and (Output[K] <= ' ') do
          K := K - 1;
        if (K > 0) and (Output[K] = ',') then
          Delete(Output, K, 1);
      end;
      JustRemoved := False;
      if InSection and (T[1] = '}') then
        InSection := False;
    end;
    Output := Output + Line;
  end;
  Text := Output;
end;

procedure RemoveOldTrackingOverrides();
var
  FileName, Text: String;
  Raw: AnsiString;
  Removed, Unhandled: Integer;
begin
  FileName := FindSteamVRSettings();
  if (FileName = '') or not LoadStringFromFile(FileName, Raw) then
    Exit;
  Text := Utf8Decode(Raw);
  if Pos(OldOverride, Text) = 0 then
    Exit;
  Removed := 0;
  Unhandled := 1;
  if Utf8Encode(Text) = Raw then          // edit only what is written back byte for byte
    Removed := StripOldOverrides(Text, Unhandled);
  if Removed > 0 then
  begin
    if CopyFile(FileName, FileName + '.cyberfinger-backup', False) and
       SaveStringToFile(FileName, Utf8Encode(Text), False) then
      Log('Removed ' + IntToStr(Removed) + ' CyberFinger TrackingOverrides from ' + FileName +
          ' (backup: ' + FileName + '.cyberfinger-backup)')
    else
      Unhandled := Unhandled + Removed;
  end;
  if Unhandled > 0 then
    SuppressibleMsgBox('Your SteamVR settings contain TrackingOverrides for the old CyberFinger driver. Please remove ' +
                       'the "/devices/cyberfinger/..." entries from "TrackingOverrides" in' + #13#10 + FileName + #13#10 +
                       'while SteamVR is closed: they would hide your hands whenever the CyberFinger driver is off.',
                       mbInformation, MB_OK, IDOK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssInstall) and WizardIsTaskSelected('steamvrdriver') then
  begin
    RemoveDriverInsideSteamVR();
    RemoveOldTrackingOverrides();
  end;
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('steamvrdriver') and not HaveVRPathReg() then
    SuppressibleMsgBox('SteamVR was not found, so the CyberFinger driver is installed but not registered.' + #13#10 +
                       'Install SteamVR, start it once, then run:' + #13#10 +
                       'vrpathreg adddriver "' + ExpandConstant('{app}\SteamVR\cyberfinger') + '"',
                       mbInformation, MB_OK, IDOK);
#if HaveResoniteMods
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('resonite') then
  begin
    LinkResoniteBuilds();
    InstallResoniteMods();
  end;
#endif
end;

// ── ViGEmBus ───────────────────────────────────────────────────────────────

function IsViGEmInstalled: Boolean;
var
  RegistryStr: String;
begin
  // Check if ViGEmBus is already installed via registry
  Result := False;
  if RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{B37B7390-6E44-4D67-8FC6-565B8A1E58FF}_is1',
     'DisplayName', RegistryStr) then
  begin
    Result := True;
  end;
  // Also check alternative registry path
  if not Result then
  begin
    if RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\{B37B7390-6E44-4D67-8FC6-565B8A1E58FF}_is1',
       'DisplayName', RegistryStr) then
    begin
      Result := True;
    end;
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpSelectTasks then
  begin
    if IsViGEmInstalled then
    begin
      Log('ViGEmBus already installed');
    end;
  end;
end;

[Messages]
WelcomeLabel2=This will install [name] on your computer.%n%n{#MyAppName} connects CyberFinger BLE controllers to your PC as SteamVR controllers or an Xbox 360 gamepad. It includes the CyberFinger SteamVR driver, which you can switch off in SteamVR Settings > Startup/Shutdown > Manage Add-ons, and can add the CyberFinger mods to Resonite. An existing installation is upgraded in place.%n%nPlease quit SteamVR before continuing.
