; Inno Setup script for DiscDock.
; Build it with scripts\build-installer.ps1, which passes the version number.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "DiscDock"
#define AppExe "DiscDock.exe"
#define ReleaseDir "..\release\DiscDock"

[Setup]
AppId={{6B1E8F3A-4C2D-4E7B-9A51-3D2C8E6F7A10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
LicenseFile=..\LICENSE
OutputDir=..\release\installer
OutputBaseFilename=DiscDock-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
CloseApplications=no
SetupLogging=yes
SetupIconFile=..\assets\discdock.ico

[Tasks]
Name: "startup"; Description: "Start DiscDock when I sign in to Windows"; GroupDescription: "Startup:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked
Name: "mediatools"; Description: "Install FFmpeg, HandBrake CLI and cyanrip with winget (needs internet, accepts their licenses)"; GroupDescription: "Helper tools:"; Flags: unchecked

[InstallDelete]
; Remove libraries of the previous version so no stale files are left behind.
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\launch-installed.ps1"

[Files]
Source: "{#ReleaseDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--open-browser"; WorkingDir: "{app}"; Comment: "Open the DiscDock dashboard"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--open-browser"; WorkingDir: "{app}"; Comment: "Open the DiscDock dashboard"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppExe}"" --background"; Flags: uninsdeletevalue; Tasks: startup
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "{#AppName}"; Flags: deletevalue; Tasks: not startup

[Run]
Filename: "{cmd}"; Parameters: "/c winget install --id Gyan.FFmpeg --exact --silent --accept-package-agreements --accept-source-agreements & winget install --id HandBrake.HandBrake.CLI --exact --silent --accept-package-agreements --accept-source-agreements & winget install --id cyanreg.cyanrip --exact --silent --accept-package-agreements --accept-source-agreements"; StatusMsg: "Installing FFmpeg, HandBrake CLI and cyanrip..."; Flags: waituntilterminated; Tasks: mediatools
; DiscDock that was running before the update starts again, and Setup waits until the new version answers.
Filename: "{app}\{#AppExe}"; Parameters: "--background"; WorkingDir: "{app}"; StatusMsg: "Starting DiscDock again..."; Flags: nowait; Check: WasRunningBeforeUpdate; AfterInstall: CheckRestartedVersion
Filename: "{app}\{#AppExe}"; Parameters: "--open-browser"; Description: "Open DiscDock"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/IM {#AppExe} /F"; Flags: runhidden waituntilterminated; RunOnceId: "StopDiscDock"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN DiscDock /F"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveScheduledTask"

[Code]
var
  WasRunning: Boolean;

{ The body of a GET request to the DiscDock running on this computer, or '' when it does not answer. }
function ServiceGet(Path: String; ReceiveTimeout: Integer): String;
var
  Request: Variant;
begin
  Result := '';
  try
    Request := CreateOleObject('WinHttp.WinHttpRequest.5.1');
    Request.SetTimeouts(2000, 2000, 2000, ReceiveTimeout);
    Request.Open('GET', 'http://127.0.0.1:8199' + Path, False);
    Request.Send('');
    if Request.Status = 200 then
      Result := Request.ResponseText;
  except
    { DiscDock is not running or not answering. }
    Result := '';
  end;
end;

{ True while DiscDock reports a disc job that must not be interrupted. }
function DiscJobIsRunning(): Boolean;
var
  Body: String;
  States: TArrayOfString;
  Index: Integer;
begin
  Result := False;
  States := ['detected', 'inspecting', 'identifying', 'queued', 'ripping', 'ripped', 'verifying',
             'transcoding', 'finalizing', 'ejecting', 'cancelling'];
  Body := ServiceGet('/api/v1/bootstrap', 10000);
  for Index := 0 to GetArrayLength(States) - 1 do
    if Pos('"state":"' + States[Index] + '"', Body) > 0 then
    begin
      Result := True;
      exit;
    end;
end;

{ DiscDock holds this mutex while it runs, so Setup needs no process list to know. }
function DiscDockRunning(): Boolean;
begin
  Result := CheckForMutexes('Local\DiscDockNativeService');
end;

{ Asks the DiscDock running on this computer to close itself. Versions before 1.8.2 do not know the request. }
function AskDiscDockToClose(): Boolean;
var
  Request: Variant;
begin
  Result := False;
  try
    Request := CreateOleObject('WinHttp.WinHttpRequest.5.1');
    Request.SetTimeouts(2000, 2000, 2000, 10000);
    Request.Open('POST', 'http://127.0.0.1:8199/api/v1/shutdown', False);
    Request.SetRequestHeader('X-DiscDock-Request', 'close');
    Request.Send('');
    Result := Request.Status = 202;
  except
    Result := False;
  end;
end;

{ Waits until DiscDock has closed; Windows releases its files only then. }
function WaitUntilDiscDockStopped(Seconds: Integer): Boolean;
var
  Checks: Integer;
begin
  Checks := 0;
  while DiscDockRunning() and (Checks < Seconds * 2) do
  begin
    Sleep(500);
    Checks := Checks + 1;
  end;
  Result := not DiscDockRunning();
end;

{ The version reported by the DiscDock that answers within Seconds, or '' when none does. }
function AnsweringVersion(Seconds: Integer): String;
var
  Body: String;
  Start, Stop, Checks: Integer;
begin
  Result := '';
  Checks := 0;
  while Checks < Seconds * 2 do
  begin
    Body := ServiceGet('/api/v1/health', 2000);
    Start := Pos('"version":"', Body);
    if Start > 0 then
    begin
      Body := Copy(Body, Start + Length('"version":"'), 40);
      Stop := Pos('"', Body);
      if Stop > 0 then
        Result := Copy(Body, 1, Stop - 1);
      exit;
    end;
    Sleep(500);
    Checks := Checks + 1;
  end;
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  { Setup installs for the Windows account it runs as. "Run as administrator" with another account's
    password would update that account's copy of DiscDock, not yours. }
  if IsAdmin() and not WizardSilent() then
    Result := MsgBox('Setup is running as administrator, as the Windows account "' + GetUserNameString() +
      '", and installs DiscDock for that account.' + #13#10#13#10 +
      'If you use DiscDock with another Windows account, choose No and start Setup again without "Run as administrator".' + #13#10#13#10 +
      'Continue?', mbConfirmation, MB_YESNO) = IDYES;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  Closed: Boolean;
begin
  Result := '';
  if DiscJobIsRunning() then
  begin
    Result := 'DiscDock is working on a disc. Let the job finish or stop it in the dashboard, then run Setup again.';
    exit;
  end;
  WasRunning := DiscDockRunning();
  if WasRunning then
  begin
    { DiscDock closes itself when asked. Ending its process from Setup is what antivirus behaviour
      monitoring flags, so that is only done for versions before 1.8.2, which cannot be asked. }
    Log('Asking DiscDock to close so its files can be replaced');
    Closed := False;
    if AskDiscDockToClose() then
      Closed := WaitUntilDiscDockStopped(30);
    if not Closed then
    begin
      Log('DiscDock did not close by itself; ending it');
      Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM {#AppExe} /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
    if not WaitUntilDiscDockStopped(30) then
    begin
      Result := 'DiscDock is still running and Setup could not close it, so its files cannot be updated. ' +
        'Close DiscDock in Task Manager, or restart the computer, then run Setup again.';
      exit;
    end;
    { A process that has just ended can still hold its files for a moment. }
    Sleep(1000);
  end;
end;

function WasRunningBeforeUpdate(): Boolean;
begin
  Result := WasRunning;
end;

procedure CheckRestartedVersion();
var
  Running: String;
begin
  Running := AnsweringVersion(60);
  if Running = '' then
    Log('DiscDock did not answer within 60 seconds after the update; it may still be starting')
  else
  begin
    Log('DiscDock answering after the update: ' + Running);
    if Running <> '{#AppVersion}' then
      SuppressibleMsgBox('DiscDock ' + Running + ' is still answering instead of version {#AppVersion}. ' +
        'Restart the computer to finish the update.', mbError, MB_OK, IDOK);
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  if DiscJobIsRunning() and not UninstallSilent() then
    Result := MsgBox('DiscDock is working on a disc. Uninstalling now stops that job. Continue?',
      mbConfirmation, MB_YESNO) = IDYES;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent() then
    MsgBox('DiscDock was removed. Your ripped media, settings and logs in the DiscDock folder in your user profile were kept.',
      mbInformation, MB_OK);
end;
