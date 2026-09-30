@echo off
rem Keep this file ASCII with CRLF line endings.
call "%~dp0tools\windows_entry.bat" check %*
exit /b %ERRORLEVEL%
