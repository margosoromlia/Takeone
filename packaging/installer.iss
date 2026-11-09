; Inno Setup script for Takeone (Windows installer).
; Built by build_windows.ps1:  ISCC /DAppVersion=1.0.0 /DBuildDir=<path\to\build> installer.iss

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef BuildDir
  #define BuildDir "..\build"
#endif

[Setup]
AppId={{3A9D5F17-C2E4-4B8A-A6F1-7D0E2C5B9A44}
AppName=Takeone
AppVersion={#AppVersion}
AppPublisher=Takeone
DefaultDirName={autopf}\Takeone
DefaultGroupName=Takeone
DisableProgramGroupPage=yes
OutputDir={#BuildDir}\installer
OutputBaseFilename=Takeone-Setup-{#AppVersion}
SetupIconFile={#BuildDir}\icon.ico
UninstallDisplayIcon={app}\Takeone.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
CloseApplications=yes

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#BuildDir}\dist\Takeone\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Takeone"; Filename: "{app}\Takeone.exe"
Name: "{autodesktop}\Takeone"; Filename: "{app}\Takeone.exe"; Tasks: desktopicon

[Run]
; Allow the AirPlay receiver through Windows Firewall, otherwise the iPhone cannot see it
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Takeone AirPlay"""; Flags: runhidden; Check: HasUxPlay
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Takeone AirPlay"" dir=in action=allow program=""{app}\_internal\bin\uxplay\uxplay.exe"" enable=yes profile=any"; Flags: runhidden; Check: HasUxPlay
Filename: "{app}\Takeone.exe"; Description: "{cm:LaunchProgram,Takeone}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Takeone AirPlay"""; Flags: runhidden; RunOnceId: "RemoveFirewallRule"

[Code]
function HasUxPlay: Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\_internal\bin\uxplay\uxplay.exe'));
end;
