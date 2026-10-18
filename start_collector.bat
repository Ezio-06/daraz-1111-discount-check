@echo off
rem Starts the Daraz collector in the background: one sweep every 6 hours until shutdown.
rem Put a shortcut to this file in your Startup folder (Win+R, type shell:startup) so it runs at every login.
rem Safe to double-click again: a second copy notices the first one and exits.
rem After a restart it checks the database and only sweeps if the last sweep is 6+ hours old.
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0collector.py" --loop 21600
