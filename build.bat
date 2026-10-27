@echo off
echo ========================================================
echo       GERANDO EXECUTAVEL: MonitorNOC.exe
echo ========================================================
echo.

if exist build rmdir /s /q build 2>nul

python -m PyInstaller --noconfirm --onefile --noconsole --icon="icon.ico" --add-data "templates;templates" --add-data "icon.ico;." --hidden-import "pystray._win32" --name "MonitorNOC" app.py

if exist LICENSE.txt copy /y LICENSE.txt dist\ >nul

echo.
echo ========================================================
echo  CONCLUIDO! O executavel esta na pasta: dist\MonitorNOC.exe
echo  Licenca copiada para: dist\LICENSE.txt
echo ========================================================
pause
