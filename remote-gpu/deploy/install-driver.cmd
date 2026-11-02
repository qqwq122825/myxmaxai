@echo off
setlocal
set "RGPU_PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "RGPU_PS=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
"%RGPU_PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-driver.ps1"
set "RGPU_EXIT=%ERRORLEVEL%"
echo.
echo Exit code: %RGPU_EXIT%. See the Report path above for details.
pause
exit /b %RGPU_EXIT%
