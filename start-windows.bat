@echo off
rem Skills Hub - start the local manager (http://127.0.0.1:7799)
cd /d "%~dp0"
where py >nul 2>nul && (py webui.py) || (python webui.py)
