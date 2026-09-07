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
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Per-user install: no admin rights, no UAC prompt (see header).
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
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
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
