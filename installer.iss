[Setup]
AppName=PyDM
AppVersion=1.0
AppPublisher=PyDM
DefaultDirName={autopf}\PyDM
DefaultGroupName=PyDM
OutputDir=installer
OutputBaseFilename=PyDM-Setup
Compression=lzma
SolidCompression=yes
UninstallDisplayIcon={app}\PyDM.exe
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na área de trabalho"

[Files]
Source: "dist\PyDM.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\PyDM"; Filename: "{app}\PyDM.exe"
Name: "{autodesktop}\PyDM"; Filename: "{app}\PyDM.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\PyDM.exe"; Description: "Abrir o PyDM"; Flags: nowait postinstall skipifsilent
