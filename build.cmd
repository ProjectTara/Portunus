@echo off
rem Build a standalone Portunus folder with PyInstaller.
rem Run from a terminal inside an activated virtual environment that already
rem has requirements.txt installed. This script does not change execution
rem policy, unblock files, or download anything except PyInstaller itself.
setlocal
python -m pip install --require-hashes -r requirements-build.lock || exit /b 1
python -m PyInstaller --noconfirm --clean --windowed --name Portunus ^
  --add-data "portunus\ui;portunus\ui" ^
  --collect-all pymobiledevice3 ^
  --collect-all pytun_pmd3 ^
  --collect-data pykdebugparser ^
  --recursive-copy-metadata pymobiledevice3 ^
  --collect-submodules winrt ^
  portunus\__main__.py || exit /b 1
echo.
echo Selesai: dist\Portunus\Portunus.exe
