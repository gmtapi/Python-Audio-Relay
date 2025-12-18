@echo off
echo Building Mic Relayer EXE...
echo.

echo Installing/updating dependencies...
python -m pip install -r requirements.txt

echo.
echo Building EXE with PyInstaller...
set EXE_NAME=AMD ReLive
set ICON_FILE=logo.ico

python -m PyInstaller --onefile --name "%EXE_NAME%" --icon="%ICON_FILE%" --noconsole --add-binary "opus.dll;." bomb.py

echo.
echo If there were no errors above, the EXE is in the 'dist' folder.
echo.
pause
