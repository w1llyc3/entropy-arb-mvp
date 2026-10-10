@echo off
REM Single watchdog for the localhost panel. A second copy of this file exits.
REM If 127.0.0.1:8765 already answers (TCP listen or HTTP /api/status), the
REM helper does not spawn another "python -m web" (that is WinError 10048).
REM It still starts the panel when the port is actually down, and restarts
REM after that child exits and the port is still down.
setlocal
cd /d "%~dp0.."
python -m web.watchdog %*
exit /b %ERRORLEVEL%
