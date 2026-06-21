; Inno Setup script for the LapSmith installer (a tuning tool for Forza Horizon 6).
;
; Build order:
;   1. powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
;      (produces dist\LapSmith\, the portable zip, AND runs this script if ISCC is
;       on PATH or installed in the default location)
;   2. or manually:
;      "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyAppVersion=0.1.0 packaging\lapsmith.iss
;
; The version is passed in with /DMyAppVersion so it stays in sync with the single
; source of truth, lapsmith/__init__.py __version__ (see build_windows.ps1).
; Paths are relative to THIS file (packaging\).

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "LapSmith"
#define MyAppExeName "LapSmith.exe"
#define MyAppPublisher "LapSmith"
#define MyAppDist "..\dist\LapSmith"
#define MyAppIcon "..\lapsmith\assets\lapsmith.ico"
#define MyAppNotices "..\THIRD-PARTY-NOTICES.txt"

[Setup]
; A stable AppId keeps upgrades/uninstall clean across versions.
AppId={{B7E6F2A1-9C3D-4E5F-A6B8-1D2C3E4F5A6B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
OutputDir=..\dist
OutputBaseFilename=LapSmith-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Icon for the installer .exe itself.
SetupIconFile={#MyAppIcon}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "runadmin"; Description: "Always run as administrator (needed for global hotkeys while the game is focused)"; GroupDescription: "Options:"

[Files]
; Ship the entire self-contained one-folder build...
Source: "{#MyAppDist}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs
; ...and the third-party notices alongside it.
Source: "{#MyAppNotices}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\Third-party notices"; Filename: "{app}\THIRD-PARTY-NOTICES.txt"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; If the user opted in, set the per-user "run as administrator" compatibility flag
; so the app always launches elevated (global hotkeys need it). Removed on uninstall.
Root: HKCU; Subkey: "Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"; \
    ValueType: string; ValueName: "{app}\{#MyAppExeName}"; ValueData: "~ RUNASADMIN"; \
    Flags: uninsdeletevalue; Tasks: runadmin

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
