; ID da extensão na Chrome Web Store (os 32 caracteres da URL da extensão na loja).
; Vazio = o instalador NÃO registra a extensão no Chrome e usa só o passo a passo manual.
; Preenchido = o Chrome instala a extensão da loja na próxima abertura e pede para o usuário clicar em "Ativar extensão".
#define ChromeExtId ""

[Setup]
AppName=Raio Gerenciador de Downloads
AppVersion=1.0
AppPublisher=Raio
DefaultDirName={autopf}\Raio
DefaultGroupName=Raio
OutputDir=installer
OutputBaseFilename=Raio-Setup
Compression=lzma
SolidCompression=yes
UninstallDisplayIcon={app}\Raio.exe
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na área de trabalho"
Name: "videocomponents"; Description: "Baixar os componentes para baixar vídeos de sites (yt-dlp, Deno e ffmpeg - cerca de 250 MB, precisa de internet)"

[Files]
Source: "dist\Raio.exe"; DestDir: "{app}"; Flags: ignoreversion
; extensão do navegador (pasta "extension" ao lado deste arquivo, com o manifest.json dentro)
Source: "extension\*"; DestDir: "{app}\extension"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Raio"; Filename: "{app}\Raio.exe"
Name: "{autodesktop}\Raio"; Filename: "{app}\Raio.exe"; Tasks: desktopicon

#if ChromeExtId != ""
[Registry]
; o Chrome lê a chave da visão de 32 bits (Wow6432Node) e, em alguns casos, a de 64 bits: gravamos nas duas
Root: HKLM; Subkey: "Software\Google\Chrome\Extensions\{#ChromeExtId}"; ValueType: string; ValueName: "update_url"; ValueData: "https://clients2.google.com/service/update2/crx"; Flags: 32bit uninsdeletekey
Root: HKLM; Subkey: "Software\Google\Chrome\Extensions\{#ChromeExtId}"; ValueType: string; ValueName: "update_url"; ValueData: "https://clients2.google.com/service/update2/crx"; Flags: uninsdeletekey
#endif

[Run]
; roda como o usuário que abriu o instalador (e não como o administrador): os componentes vão para a pasta .pydm dele
Filename: "{app}\Raio.exe"; Parameters: "--install-components"; StatusMsg: "Baixando os componentes de vídeo..."; Tasks: videocomponents; Flags: runasoriginaluser skipifsilent
; Chrome e Edge não deixam instaladores colocarem extensões sozinhos: este passo a passo leva o usuário até a pasta certa.
; Também como o usuário original (abrir o navegador como administrador não é desejável).
#if ChromeExtId == ""
Filename: "{app}\Raio.exe"; Parameters: "--install-extension"; Description: "Instalar a extensão no navegador (abre um passo a passo)"; Flags: runasoriginaluser nowait postinstall skipifsilent
#else
; com o ID, o Chrome já instala sozinho; o passo a passo fica como alternativa (Edge, outros navegadores)
Filename: "{app}\Raio.exe"; Parameters: "--install-extension"; Description: "Ver o passo a passo da extensão (Edge e outros navegadores)"; Flags: runasoriginaluser nowait postinstall skipifsilent unchecked
#endif
Filename: "{app}\Raio.exe"; Description: "Abrir o Raio"; Flags: nowait postinstall skipifsilent

[Code]
// Ao desinstalar: fecha o Raio (senão o Raio.exe fica em uso), apaga os componentes de vídeo baixados
// (yt-dlp, Deno, ffmpeg em %USERPROFILE%\.pydm\bin) e remove o "iniciar com o Windows".
// A lista de downloads e as configurações (%USERPROFILE%\.pydm) são mantidas.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM Raio.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(800);
    DelTree(ExpandConstant('{%USERPROFILE}') + '\.pydm\bin', True, True, True);
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Raio');
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'PyDM');   // nome usado antes da troca de nome
  end;
end;
