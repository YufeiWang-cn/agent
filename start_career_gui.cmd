@echo off
setlocal
python "%~dp0_bootstrap.py" --launch career_gui.py %*
exit /b %errorlevel%
