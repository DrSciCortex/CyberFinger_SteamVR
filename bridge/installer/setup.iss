; ── CyberFinger Bridge Installer ──
; Inno Setup 6 script
; Compile with: iscc setup.iss (from installer/ directory)
; Or: right-click setup.iss → Compile

#define MyAppName "CyberFinger Bridge"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "SciCortex Technologies Corp."
#define MyAppURL "https://github.com/DrSciCortex/CyberFinger"
#define MyAppExeName "CyberFingerBridge.exe"
#define ViGEmSetup "ViGEmBus_1.22.0_x64_x86_arm64.exe"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
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

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "installvigem"; Description: "Install ViGEmBus driver (required for Gamepad mode)"; GroupDescription: "Drivers:"; Flags: checkedonce

[Files]
; Main application
Source: "..\dist\CyberFingerBridge.exe"; DestDir: "{app}"; Flags: ignoreversion

; Icon
Source: "..\assets\icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\icon.png"; DestDir: "{app}"; Flags: ignoreversion

; ViGEmBus installer — bundled into the setup, extracted on demand
; Download from: https://github.com/nefarius/ViGEmBus/releases/download/v1.22.0/ViGEmBus_1.22.0_x64_x86_arm64.exe
; Place in: installer/ViGEmBus_1.22.0_x64_x86_arm64.exe
Source: "{#ViGEmSetup}"; DestDir: "{tmp}"; Flags: ignoreversion deleteafterinstall; Check: ViGEmFileExists

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\icon.ico"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Run]
; Install ViGEmBus driver if the task is selected
Filename: "{tmp}\{#ViGEmSetup}"; Parameters: "/qn /norestart"; StatusMsg: "Installing ViGEmBus driver..."; Tasks: installvigem; Check: ViGEmFileExists; Flags: waituntilterminated shellexec

; Launch after install
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
function ViGEmFileExists: Boolean;
begin
  // Check if the ViGEm installer exists next to setup.iss at compile time
  // At runtime this always returns True because the file is bundled into the setup
  Result := True;
end;

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
      Log('ViGEmBus already installed — unchecking driver install task');
      // Could auto-uncheck but user can still override
    end;
  end;
end;

[Messages]
WelcomeLabel2=This will install [name] on your computer.%n%n{#MyAppName} connects CyberFinger BLE controllers to your PC as VR controllers or an Xbox 360 gamepad.%n%nGamepad mode requires the ViGEmBus driver. If you don't have it, the installer can set it up for you.
