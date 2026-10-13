@echo off
rem Keep this file ASCII with CRLF line endings.
call "%~dp0..\windows_entry.bat" diagnose %*
exit /b %ERRORLEVEL%
