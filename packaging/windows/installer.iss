; Inno Setup script: iscc /DAppVersion=<version> /DSourceDir=<dist\DvdPlayer> /DOutputName=<name> installer.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef NumVersion
  #define NumVersion "0.0.0"
#endif
#ifndef OutputName
  #define OutputName "DvdPlayer-setup"
#endif

[Setup]
AppId={{6B0C5E0E-2A3B-4C1D-9C55-1D8A4F0B2D11}
AppName=DVD Player
AppVersion={#AppVersion}
VersionInfoVersion={#NumVersion}
AppPublisher=cuisinecometwot
AppPublisherURL=https://github.com/cuisinecometwot/dvd-player
DefaultDirName={autopf}\DVD Player
DefaultGroupName=DVD Player
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\DvdPlayer.exe
OutputDir=Output
OutputBaseFilename={#OutputName}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\..\LICENSE
PrivilegesRequiredOverridesAllowed=dialog

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\DVD Player"; Filename: "{app}\DvdPlayer.exe"
Name: "{autodesktop}\DVD Player"; Filename: "{app}\DvdPlayer.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\DvdPlayer.exe"; Description: "Start DVD Player"; Flags: nowait postinstall skipifsilent
