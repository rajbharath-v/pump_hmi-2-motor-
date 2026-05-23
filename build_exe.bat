@echo off
echo ============================================
echo   BUILDING PUMP HMI — PROFESSIONAL EXE
echo ============================================

echo Installing dependencies...
pip install pymodbus pyserial pyinstaller

echo.
echo Building EXE...
pyinstaller --onefile ^
            --windowed ^
            --name "PumpControlSystem" ^
            --add-data "pump_settings.json;." ^
            pump_hmi.py

echo.
echo ============================================
echo   BUILD COMPLETE!
echo   Find your EXE in:  dist\PumpControlSystem.exe
echo ============================================
pause
