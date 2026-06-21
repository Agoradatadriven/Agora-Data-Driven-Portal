@echo off
rem ============================================================================
rem  Agora portal -- LOCAL PREVIEW (no password)
rem
rem  Double-click this file to run the whole portal on your own laptop:
rem    * isolated venv, throwaway local data folder -- never touches the live site
rem    * seeded with demo clients + Atrium workspaces
rem    * auto-signed-in as super-admin, so there is NO login and you can edit
rem      every workspace in place
rem
rem  A browser tab opens at http://localhost:8080 automatically. Edit the files
rem  under services\portal\dash\ and refresh to see changes -- no deploy, no push.
rem  Close this window (or press Ctrl+C) to stop.
rem ============================================================================
title Agora Portal - Local Preview (no password)
echo Starting the Agora portal locally...
echo A browser tab will open at http://localhost:8080 in a moment.
echo Close this window or press Ctrl+C to stop.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\services\portal\dash\run_local.ps1"
echo.
echo The local portal has stopped.
pause
