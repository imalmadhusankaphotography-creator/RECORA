@echo off
setlocal
cd /d "%~dp0"

echo [1/4] Installing Python packages...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller || goto :fail

if exist ffmpeg.exe goto :build
echo [2/4] Downloading FFmpeg (about 100 MB)...
curl -L -o ffmpeg.zip https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip || goto :fail
if exist ffmpeg_dl rmdir /s /q ffmpeg_dl
mkdir ffmpeg_dl
tar -xf ffmpeg.zip -C ffmpeg_dl || goto :fail
for /r ffmpeg_dl %%f in (ffmpeg.exe) do if exist "%%f" copy /y "%%f" ffmpeg.exe >nul
rmdir /s /q ffmpeg_dl
del ffmpeg.zip
if not exist ffmpeg.exe goto :fail

:build
echo [3/4] Building RECORA.exe...
python -m PyInstaller --noconfirm --clean --windowed --name RECORA --icon recora.ico --add-data "recora.ico;." recora.py || goto :fail
copy /y ffmpeg.exe dist\RECORA\ffmpeg.exe >nul
copy /y README.md dist\RECORA\README.md >nul

echo [4/4] Done:  dist\RECORA\RECORA.exe
explorer dist\RECORA
goto :eof

:fail
echo.
echo Build failed - check the messages above.
pause
