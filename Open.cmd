@echo off
if exist "%USERPROFILE%\.gemini\config\plugins\ant-extend\scripts\run.cmd" (
  call "%USERPROFILE%\.gemini\config\plugins\ant-extend\scripts\run.cmd" --open
) else (
  call "%~dp0scripts\run.cmd" --open
)
if errorlevel 1 pause
