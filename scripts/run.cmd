@echo off
setlocal
if defined ANT_EXTEND_PYTHON (
  "%ANT_EXTEND_PYTHON%" -B "%~dp0retry.py" %*
  exit /b
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -B "%~dp0retry.py" %*
  exit /b
)
where python >nul 2>nul
if not errorlevel 1 (
  python -B "%~dp0retry.py" %*
  exit /b
)
echo Python 3.10 or later is required. Install from python.org, then run again. 1>&2
exit /b 2
