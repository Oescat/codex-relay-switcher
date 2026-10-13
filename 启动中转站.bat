@echo off
setlocal
set "PYTHONW=%LocalAppData%\Programs\Python\Python313\pythonw.exe"
set "APP=%~dp0relay_switcher.pyw"

if not exist "%PYTHONW%" (
  echo Python 3.13 was not found at:
  echo %PYTHONW%
  pause
  exit /b 1
)

if not exist "%APP%" (
  echo relay_switcher.pyw was not found next to this launcher.
  pause
  exit /b 1
)

start "" "%PYTHONW%" "%APP%"
