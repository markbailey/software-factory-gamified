@echo off
rem Builds dist\SoftwareFactoryServer.exe (see build_exe.py)
cd /d "%~dp0"
.venv\Scripts\python build_exe.py
pause
