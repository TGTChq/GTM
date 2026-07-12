@echo off
setlocal
cd /d "%~dp0"
py -3 run_approved.py
exit /b %ERRORLEVEL%
