@echo off
setlocal
python "%~dp0_bootstrap.py" --launch career.py %*
exit /b %errorlevel%
