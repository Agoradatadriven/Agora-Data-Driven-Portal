@echo off
rem ============================================================================
rem  Agora portal -- LOCAL PREVIEW (WITH the real login page)
rem
rem  Same local preview as "Preview Portal.cmd", but it shows the REAL login
rem  page instead of auto-signing you in. Use it to see the CLIENT experience:
rem  sign in with ANY email + a client password and you land in that client's
rem  own view (no admin controls).
rem
rem    Client passwords (any email works -- it's just a label):
rem      riverdance-demo   honeytribe-demo   meloyelo-demo   rhe-demo
rem
rem  Runs on http://localhost:8081 so it can sit ALONGSIDE the no-login
rem  super-admin preview (Preview Portal.cmd) on 8080. Close this window or
rem  press Ctrl+C to stop.
rem ============================================================================
title Agora Portal - Local Preview (with login)
echo Starting the Agora portal locally WITH a login page (http://localhost:8081).
echo Sign in with ANY email + a client password (e.g. riverdance-demo).
echo Close this window or press Ctrl+C to stop.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0agora-platform\dash\run_local.ps1" -WithLogin -Port 8081
echo.
echo The local portal (with login) has stopped.
pause
