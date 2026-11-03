@echo off
echo === Instalando PyInstaller ===
py -m pip install --upgrade pyinstaller pystray pillow || goto erro
echo === Gerando PyDM.exe ===
py -m PyInstaller --noconsole --onefile --hidden-import pystray._win32 --name PyDM pydm.py || goto erro
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist %ISCC% goto semInno
echo === Gerando instalador ===
%ISCC% installer.iss || goto erro
echo.
echo PRONTO: installer\PyDM-Setup.exe
pause
exit /b 0
:semInno
echo.
echo PyDM.exe gerado em dist\PyDM.exe
echo Para criar o instalador, instale o Inno Setup 6 e rode este arquivo de novo:
echo https://jrsoftware.org/isdl.php
pause
exit /b 0
:erro
echo Ocorreu um erro.
pause
exit /b 1
