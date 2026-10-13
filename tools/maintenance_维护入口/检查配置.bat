@echo off
rem Keep this file ASCII with CRLF line endings.
call "%~dp0..\windows_entry.bat" check %*
exit /b %ERRORLEVEL%
