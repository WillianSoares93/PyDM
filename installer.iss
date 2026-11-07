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
; extensão do navegador (pasta "extension" ao lado deste arquivo, com o manifest.json dentro)
Source: "extension\*"; DestDir: "{app}\extension"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\PyDM"; Filename: "{app}\PyDM.exe"
Name: "{autodesktop}\PyDM"; Filename: "{app}\PyDM.exe"; Tasks: desktopicon

[Run]
; roda como o usuário que abriu o instalador (e não como o administrador): os componentes vão para a pasta .pydm dele
Filename: "{app}\PyDM.exe"; Parameters: "--install-components"; StatusMsg: "Baixando os componentes de vídeo..."; Tasks: videocomponents; Flags: runasoriginaluser skipifsilent
; Chrome e Edge não deixam instaladores colocarem extensões sozinhos: este passo a passo leva o usuário até a pasta certa.
; Também como o usuário original (abrir o navegador como administrador não é desejável).
Filename: "{app}\PyDM.exe"; Parameters: "--install-extension"; Description: "Instalar a extensão no navegador (abre um passo a passo)"; Flags: runasoriginaluser nowait postinstall skipifsilent
Filename: "{app}\PyDM.exe"; Description: "Abrir o PyDM"; Flags: nowait postinstall skipifsilent

[Code]
// Ao desinstalar: fecha o PyDM (senão o PyDM.exe fica em uso), apaga os componentes de vídeo baixados
// (yt-dlp, Deno, ffmpeg em %USERPROFILE%\.pydm\bin) e remove o "iniciar com o Windows".
// A lista de downloads e as configurações (%USERPROFILE%\.pydm) são mantidas.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM PyDM.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(800);
    DelTree(ExpandConstant('{%USERPROFILE}') + '\.pydm\bin', True, True, True);
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'PyDM');
  end;
end;
