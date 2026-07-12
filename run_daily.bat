@echo off
setlocal
cd /d "%~dp0"
py -3 run_daily.py
exit /b %ERRORLEVEL%
