@echo off
cd /d "%~dp0"
start "" "%~dp0RemoteGpu.exe" gui --role local --preview-camera yy --portrait
