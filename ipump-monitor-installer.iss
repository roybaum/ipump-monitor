; iPump Monitor Installer Script
; Created with Inno Setup 6.0+
; Download from: https://jrsoftware.org/isdl.php

[Setup]
AppName=iPump Monitor
AppVersion=1.0.0
AppPublisher=Roy Baum
AppPublisherURL=https://github.com/roybaum/ipump-monitor
DefaultDirName={autopf}\iPump Monitor
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DefaultGroupName=iPump Monitor
DisableProgramGroupPage=no
OutputBaseFilename=iPump-Monitor-Setup-v1.0.0
OutputDir=.\dist
SetupIconFile=ipump-monitor.ico
Compression=lzma
SolidCompression=yes
PrivilegesRequired=admin
ChangesAssociations=no
CloseApplications=yes
RestartApplications=no

; Images (optional - comment out if you don't have them)
; WizardImageFile=installer-logo.bmp
; WizardSmallImageFile=installer-small.bmp

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\ipump_monitor.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "ipump-monitor.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\iPump Monitor"; Filename: "{app}\ipump_monitor.exe"; WorkingDir: "{app}"; IconFilename: "{app}\ipump-monitor.ico"
Name: "{group}\{cm:UninstallProgram,iPump Monitor}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\iPump Monitor"; Filename: "{app}\ipump_monitor.exe"; WorkingDir: "{app}"; IconFilename: "{app}\ipump-monitor.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\ipump_monitor.exe"; Description: "{cm:LaunchProgram,iPump Monitor}"; Flags: nowait postinstall skipifsilent

[UninstallRun]

[Code]
procedure InitializeWizard;
begin
  WizardForm.FinishedHeadingLabel.Caption := 'Installation Complete!';
  WizardForm.FinishedLabel.Caption := 'iPump Monitor has been successfully installed. Click Finish to launch the application.' + #13#10 + #13#10 +
    'The application will open in your default web browser on http://localhost:8080';
end;

procedure StopRunningApplication();
var
  ResultCode: Integer;
begin
  Exec(
    ExpandConstant('{cmd}'),
    '/C taskkill /F /T /IM ipump_monitor.exe >nul 2>&1',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    StopRunningApplication();
end;
