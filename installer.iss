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
Name: "videocomponents"; Description: "Baixar os componentes para baixar vídeos de sites (yt-dlp, Deno e ffmpeg - cerca de 250 MB, precisa de internet)"

[Files]
Source: "dist\PyDM.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\PyDM"; Filename: "{app}\PyDM.exe"
Name: "{autodesktop}\PyDM"; Filename: "{app}\PyDM.exe"; Tasks: desktopicon

[Run]
; roda como o usuário que abriu o instalador (e não como o administrador): os componentes vão para a pasta .pydm dele
Filename: "{app}\PyDM.exe"; Parameters: "--install-components"; StatusMsg: "Baixando os componentes de vídeo..."; Tasks: videocomponents; Flags: runasoriginaluser skipifsilent
Filename: "{app}\PyDM.exe"; Description: "Abrir o PyDM"; Flags: nowait postinstall skipifsilent
