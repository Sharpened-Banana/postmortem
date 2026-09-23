; Inno Setup script for the Postmortem desktop app's Windows installer.
;
; Compiled by .github/workflows/release-desktop.yml *after* PyInstaller
; has produced dist\Postmortem\ (the onedir COLLECT layout -- see
; postmortem.spec), turning that folder into a single Setup.exe that
; installs the app, makes Start Menu / optional desktop shortcuts, and
; registers a proper uninstaller.
;
; The plain zip is still published alongside it: some people would
; rather unzip a folder than run an installer, and a portable copy is
; genuinely useful (a USB stick, a locked-down machine). This just makes
; the ordinary path ordinary.
;
; PrivilegesRequired=lowest is deliberate. Installing per-user into
; %LOCALAPPDATA% means no UAC prompt at all, which matters for an app
; whose whole point is being easy to get running -- and this app needs
; no machine-wide anything: it only ever reads WoW's log folder and
; writes its own config/reports under the user's profile.
;
; Not signed. Windows SmartScreen will show "Windows protected your PC"
; on first run until the download builds reputation ("More info" ->
; "Run anyway"). Fixing that needs a paid code-signing certificate --
; the same deliberate scope cut as the unsigned macOS build (see the
; workflow's own header comment).

#define AppName "Postmortem"
#define AppPublisher "Sharpened Banana"
#define AppURL "https://github.com/Sharpened-Banana/postmortem"
#define AppExeName "Postmortem.exe"
; Overridden by the workflow via /DAppVersion=<tag>; the fallback keeps a
; local `iscc build\postmortem.iss` run working for testing.
#ifndef AppVersion
  #define AppVersion "dev"
#endif

[Setup]
; A stable AppId is what lets an upgrade replace the previous install
; instead of stacking a second copy -- never change it.
AppId={{8E5F1B42-7C3D-4A9E-9F21-6D0B5A7C4E13}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=
OutputDir=..\installer
OutputBaseFilename=Postmortem-Setup
SetupIconFile=postmortem.ico
UninstallDisplayIcon={app}\{#AppExeName}
; The uninstaller lives OUTSIDE {app}. The in-app updater replaces the
; whole {app} folder with a downloaded build that never contains
; unins000.exe/.dat, and the update helper on builds before 36 did not
; carry them over -- so Settings > Apps > Uninstall pointed at a file
; that no longer existed ("Windows cannot find ...unins000.exe",
; 2026-09-13). Keeping it under %APPDATA% (where the app's own settings
; already live) means no update can ever move it out from under the
; registry entry. Installing over an older layout still works: Setup
; writes a fresh uninstaller here and repairs the entry.
UninstallFilesDir={userappdata}\postmortem\uninstall
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Per-user install: no admin rights, no UAC prompt (see header). With
; lowest privileges {autopf} above resolves to the per-user
; %LOCALAPPDATA%\Programs, which the app can write to.
;
; No PrivilegesRequiredOverridesAllowed. It used to be "dialog", which
; offered "Install for all users" -- that puts the app in Program Files,
; where the in-app updater (running as the user) can never move the old
; install aside, so every self-update from such an install failed. Per-
; user is the only install mode that keeps auto-update working.
PrivilegesRequired=lowest
; The app is 64-bit only, matching the CI runner's Python. Spelled "x64"
; rather than 6.3's newer "x64compatible" so this still compiles on the
; older Inno Setup 6.x that a CI image might ship -- 6.3 accepts "x64"
; as a deprecated alias, so it works either way.
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The whole PyInstaller onedir output. recursesubdirs+createallsubdirs
; keeps _internal/ (Python runtime, pywebview's WebView2 loader, this
; app's own shell/ assets) intact -- the app will not start without it.
Source: "..\dist\Postmortem\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; The app hosts its window through .NET (pywebview -> pythonnet -> WinForms
; on CoreCLR) and needs the .NET 8 Desktop Runtime, which a fresh Windows
; install does not have -- without it the app dies at startup with
; "Failed to create a .NET runtime (coreclr)". If it's missing, the
; [Code] below downloads Microsoft's own installer to {tmp} on the Ready
; page and this runs it silently. The runtime installs machine-wide, so
; it asks for elevation itself (the one UAC prompt this installer can
; cause); a user who declines still gets the app installed, plus a clear
; dialog from the app about what's missing.
Filename: "{tmp}\dotnet-desktop-runtime.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "Installing the Microsoft .NET Desktop Runtime..."; Check: not DotNetDesktopInstalled; Flags: shellexec waituntilterminated; Verb: "runas"
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
// Setup can only replace files nobody has open. With the app still running
// (the obvious state to be in when you download an update from inside it)
// Inno's fallback is to queue the locked files for the next boot and end
// with "Setup must restart your computer" -- for no reason the app itself
// has (2026-09-13). Close it first instead: the app's window is a plain
// pywebview host that does not answer Inno's own close request reliably,
// so this is a hard kill of our own executable, nothing else.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExeName} /T', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  // taskkill exits 128 when nothing matched; that is the normal case.
  Sleep(500);
end;

// True when a .NET Desktop Runtime new enough for pythonnet's CoreCLR
// hoster (6 or later) is registered. Inno runs in 64-bit mode here, so
// HKLM is the 64-bit hive where the x64 runtime registers itself.
function DotNetDesktopInstalled(): Boolean;
var
  Names: TArrayOfString;
  I: Integer;
  Major: Integer;
begin
  Result := False;
  if RegGetValueNames(HKLM, 'SOFTWARE\dotnet\Setup\InstalledVersions\x64\sharedfx\Microsoft.WindowsDesktop.App', Names) then
    for I := 0 to GetArrayLength(Names) - 1 do
    begin
      Major := StrToIntDef(Copy(Names[I], 1, Pos('.', Names[I]) - 1), 0);
      if Major >= 6 then
        Result := True;
    end;
end;

var
  DownloadPage: TDownloadWizardPage;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing), SetupMessage(msgPreparingDesc), nil);
end;

// Fetch Microsoft's runtime installer when leaving the Ready page, so a
// download failure (offline machine) is reported before anything is
// installed. aka.ms/dotnet/8.0/... is Microsoft's stable "latest 8.0
// patch" link for the x64 Desktop Runtime.
function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpReady) and not DotNetDesktopInstalled then
  begin
    DownloadPage.Clear;
    DownloadPage.Add('https://aka.ms/dotnet/8.0/windowsdesktop-runtime-win-x64.exe', 'dotnet-desktop-runtime.exe', '');
    DownloadPage.Show;
    try
      try
        DownloadPage.Download;
        Result := True;
      except
        if DownloadPage.AbortedByUser then
          Log('.NET runtime download aborted by user')
        else
          SuppressibleMsgBox('Could not download the Microsoft .NET Desktop Runtime: ' + AddPeriod(GetExceptionMessage) + #13#10#13#10 + 'Postmortem will still be installed. Install the runtime from https://dotnet.microsoft.com/download/dotnet/8.0 before starting it.', mbInformation, MB_OK, IDOK);
        Result := True;
      end;
    finally
      DownloadPage.Hide;
    end;
  end;
end;
