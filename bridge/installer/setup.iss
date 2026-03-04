; ── CyberFinger Bridge Installer ──
; Inno Setup 6 script
; Compile with: iscc setup.iss (from installer/ directory)
; Or: right-click setup.iss → Compile

#define MyAppName "CyberFinger Bridge"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "SciCortex Technologies Corp."
#define MyAppURL "https://github.com/DrSciCortex/CyberFinger"
#define MyAppExeName "CyberFingerBridge.exe"

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
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
; Modern look
WizardImageFile=compiler:WizModernImage.bmp
WizardSmallImageFile=compiler:WizModernSmallImage.bmp

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

; ViGEmBus installer
; Download from: https://github.com/nefarius/ViGEmBus/releases
Source: "ViGEmBus_1.22.0_x64_x86_arm64.exe"; DestDir: "{tmp}"; Flags: ignoreversion dontcopy; Check: ViGEmBusExists

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\icon.ico"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Run]
; Install ViGEmBus driver if selected and available
Filename: "{tmp}\ViGEmBus_1.22.0_x64_x86_arm64.exe"; Parameters: "/quiet /norestart"; StatusMsg: "Installing ViGEmBus driver..."; Tasks: installvigem; Check: ViGEmBusExists; Flags: waituntilterminated

; Launch after install
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
function ViGEmBusExists: Boolean;
begin
  Result := FileExists(ExpandConstant('{src}\ViGEmBus_1.22.0_x64_x86_arm64.exe'));
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpSelectTasks then
  begin
    if not ViGEmBusExists then
    begin
      Log('ViGEmBus_Setup.exe not found — Gamepad mode driver task will be hidden');
    end;
  end;
end;

[Messages]
WelcomeLabel2=This will install [name] on your computer.%n%n{#MyAppName} connects CyberFinger BLE controllers to your PC as VR controllers or an Xbox 360 gamepad.%n%nGamepad mode requires the ViGEmBus driver. If you don't have it, the installer can set it up for you.
