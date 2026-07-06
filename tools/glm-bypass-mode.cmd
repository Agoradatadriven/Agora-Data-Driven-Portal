@echo off
REM Double-click to launch Claude Code on Z.ai GLM in a fresh window,
REM in the folder this .cmd lives in. Any args pass through to claude.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0glm-bypass-mode.ps1" %*
pause
