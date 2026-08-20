#define AppName "RadioTEDU OnAir"
#define AppPublisher "RadioTEDU Technologies"
#define ServiceName "RadioTEDU.OnAir.Supervisor"
#ifndef AppVersion
#error AppVersion must be supplied from the repository VERSION file by build_setup.ps1
#endif

[Setup]
AppId=RadioTEDUOnAir
AppName={#AppName}
AppPublisher={#AppPublisher}
SetupIconFile=..\app\static\icons\icon.ico
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={commonpf}\RadioTEDU\OnAir
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=RadioTEDU-OnAir-Setup-{#AppVersion}
OutputDir=..\release\setup
; Fast LZMA2 keeps the self-contained FFmpeg payload practical to rebuild on
; the broadcast PC while retaining solid compression and deterministic output.
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
WizardResizable=yes
WizardImageFile=assets\wizard-large.bmp
WizardSmallImageFile=assets\wizard-small.bmp
SetupLogging=yes
LicenseFile=..\LICENSE.md
UninstallDisplayName={#AppName}
VersionInfoCompany={#AppPublisher}
VersionInfoProductName={#AppName}
VersionInfoDescription=RadioTEDU OnAir multi-channel radio automation
CloseApplications=yes
RestartApplications=no
AppMutex=Global\RadioTEDU.OnAir.Operator

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: checkedonce
Name: "startmenuicon"; Description: "Create a Start Menu shortcut"; GroupDescription: "Shortcuts:"; Flags: checkedonce
Name: "launch"; Description: "Open RadioTEDU OnAir after installation"; GroupDescription: "After installation:"; Flags: checkedonce

[Files]
Source: "..\dist\backend\*"; DestDir: "{app}\backend"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\desktop\shell\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\desktop\supervisor\*"; DestDir: "{app}\supervisor"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE.md"; DestDir: "{app}\licenses"; Flags: ignoreversion
Source: "THIRD_PARTY_NOTICES.md"; DestDir: "{app}\licenses"; Flags: ignoreversion
Source: "EnsureDesktopPrerequisites.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "HardenServiceHostAcl.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "ConfigureCrashDumps.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "InstallSupervisorService.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "InstallAudioWatchdog.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "ProvisionBroadcastPcAgents.ps1"; DestDir: "{app}\installer\handoff"; Flags: ignoreversion
Source: "StageBroadcastPcHandoff.ps1"; DestDir: "{app}\installer\handoff"; Flags: ignoreversion
Source: "NewBroadcastPcHandoffManifest.ps1"; DestDir: "{app}\installer\handoff"; Flags: ignoreversion
Source: "requirements\radiotedu-handoff-py312.lock.txt"; DestDir: "{app}\installer\handoff\requirements"; Flags: ignoreversion
Source: "templates\unified-media-source-map.json"; DestDir: "{app}\installer\handoff\templates"; Flags: ignoreversion
Source: "..\tools\radiotedu_public_state_agent.py"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "..\tools\soak_test_onair.py"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "..\tools\RadioTEDU-AudioWatchdog.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "..\docs\*.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Dirs]
Name: "{commonappdata}\RadioTEDU\OnAir"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\Backups"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\CrashDumps"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\Logs"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\Logs\Channels"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\Logs\Supervisor"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\Media"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\Media\Songs"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\Media\Jingles"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\Media\Station IDs"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\Media\Advertisements"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\Media\Recorded Shows"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\Media\Emergency"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\Media\Fallback"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\Media\Quarantine"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\Recovery"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\Recovery\Staging"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\schema-backups"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\secrets"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\Services"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\State"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\State\Channels"; Flags: uninsneveruninstall
Name: "{commonappdata}\RadioTEDU\OnAir\State\Supervisor"; Flags: uninsneveruninstall

[Icons]
Name: "{autodesktop}\RadioTEDU OnAir"; Filename: "{app}\RadioTEDU-OnAir.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{autoprograms}\RadioTEDU OnAir"; Filename: "{app}\RadioTEDU-OnAir.exe"; WorkingDir: "{app}"; Tasks: startmenuicon

[Run]
Filename: "{app}\RadioTEDU-OnAir.exe"; Description: "Open RadioTEDU OnAir"; Flags: nowait postinstall skipifsilent; Tasks: launch

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{app}\installer\InstallAudioWatchdog.ps1"" -Action Remove -AppRoot ""{app}"" -DataRoot ""{commonappdata}\RadioTEDU\OnAir"""; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "RemoveAudioWatchdog"
Filename: "{sys}\sc.exe"; Parameters: "stop {#ServiceName}"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "StopSupervisor"
Filename: "{sys}\sc.exe"; Parameters: "delete {#ServiceName}"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "DeleteSupervisor"

[Messages]
WelcomeLabel1=Welcome to [name]
WelcomeLabel2=This installer deploys one RadioTEDU OnAir operator interface and one machine-level supervisor. Shared configuration and recovery data remain protected in ProgramData. The guided setup validates channels, media, outputs, optional AI, and recovery before broadcasting.
FinishedHeadingLabel=RadioTEDU OnAir is installed
FinishedLabel=Open RadioTEDU OnAir to complete the resumable 18-step commissioning wizard.

[Code]
const
  SupervisorServiceName = '{#ServiceName}';

var
  ServiceExistedBeforeInstall: Boolean;

function RunHidden(const FileName, Parameters, FailureMessage: string): Integer;
var
  ResultCode: Integer;
begin
  if not Exec(FileName, Parameters, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    RaiseException(FailureMessage);
  Result := ResultCode;
end;

function ServiceExists: Boolean;
begin
  Result := RunHidden(
    ExpandConstant('{sys}\sc.exe'),
    'query "' + SupervisorServiceName + '"',
    'Could not query the RadioTEDU OnAir supervisor service.') = 0;
end;

function InitializeSetup: Boolean;
begin
  ServiceExistedBeforeInstall := ServiceExists;
  Result := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): string;
begin
  if ServiceExistedBeforeInstall then
  begin
    RunHidden(
      ExpandConstant('{sys}\net.exe'),
      'stop "' + SupervisorServiceName + '" /y',
      'Could not stop the existing RadioTEDU OnAir supervisor.');
  end;
  Result := '';
end;

procedure RunPowerShell(const ScriptName, ScriptParameters, FailureMessage: string);
var
  ResultCode: Integer;
  PowerShellPath: string;
  ScriptPath: string;
  Parameters: string;
begin
  PowerShellPath := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  ScriptPath := ExpandConstant('{app}\installer\') + ScriptName;
  Parameters := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    ScriptPath + '" ' + ScriptParameters;
  ResultCode := RunHidden(PowerShellPath, Parameters, FailureMessage);
  if ResultCode <> 0 then
    RaiseException(FailureMessage);
end;

procedure RemoveNewSupervisorAfterFailure;
begin
  if not ServiceExistedBeforeInstall then
  begin
    RunHidden(ExpandConstant('{sys}\sc.exe'), 'stop "' + SupervisorServiceName + '"', '');
    RunHidden(ExpandConstant('{sys}\sc.exe'), 'delete "' + SupervisorServiceName + '"', '');
  end;
end;

procedure RollbackAudioWatchdogAfterFailure;
var
  AppRoot: string;
  DataRoot: string;
begin
  AppRoot := ExpandConstant('{app}');
  DataRoot := ExpandConstant('{commonappdata}\RadioTEDU\OnAir');
  try
    RunPowerShell(
      'InstallAudioWatchdog.ps1',
      '-Action Rollback -AppRoot "' + AppRoot + '" -DataRoot "' + DataRoot +
        '" -BackendPort 8100 -ServiceName "' + SupervisorServiceName + '"',
      'The previous audio watchdog task could not be restored.');
  except
    Log('RadioTEDU audio watchdog rollback failed: ' + GetExceptionMessage);
  end;
end;

procedure ConfigureProduct;
var
  AppRoot: string;
  DataRoot: string;
  CommonParameters: string;
begin
  AppRoot := ExpandConstant('{app}');
  DataRoot := ExpandConstant('{commonappdata}\RadioTEDU\OnAir');
  CommonParameters := '-AppRoot "' + AppRoot + '" -DataRoot "' + DataRoot +
    '" -ServiceName "' + SupervisorServiceName + '"';

  RunPowerShell(
    'InstallAudioWatchdog.ps1',
    '-Action Prepare ' + CommonParameters + ' -BackendPort 8100',
    'The existing RadioTEDU audio watchdog could not be preserved.');

  RunPowerShell(
    'InstallSupervisorService.ps1',
    '-Action Prepare ' + CommonParameters,
    'The RadioTEDU OnAir supervisor could not be configured.');
  RunPowerShell(
    'HardenServiceHostAcl.ps1',
    '-OnAirRoot "' + DataRoot + '" -ServiceName "' + SupervisorServiceName + '" -RequireServiceIdentity',
    'The RadioTEDU OnAir data ACLs could not be secured.');
  RunPowerShell(
    'ConfigureCrashDumps.ps1',
    '-DataRoot "' + DataRoot + '"',
    'Windows crash dump capture could not be configured.');
  RunPowerShell(
    'EnsureDesktopPrerequisites.ps1',
    '',
    'Microsoft WebView2 Runtime could not be verified or installed.');
  RunPowerShell(
    'InstallSupervisorService.ps1',
    '-Action Start ' + CommonParameters,
    'The RadioTEDU OnAir supervisor could not be started.');
  RunPowerShell(
    'InstallAudioWatchdog.ps1',
    '-Action Install ' + CommonParameters + ' -BackendPort 8100',
    'The independent RadioTEDU audio watchdog could not be installed.');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    try
      ConfigureProduct;
    except
      RollbackAudioWatchdogAfterFailure;
      RemoveNewSupervisorAfterFailure;
      RaiseException(GetExceptionMessage);
    end;
  end;
end;
