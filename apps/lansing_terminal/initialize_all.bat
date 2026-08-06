@echo off
rem Windows Command Prompt launcher for the shared Lansing initialization workflow.
rem Override the default interpreter by setting PYTHON_EXECUTABLE first.
setlocal

set "SCRIPT_DIR=%~dp0"
if defined PYTHON_EXECUTABLE (
    set "PYTHON_COMMAND=%PYTHON_EXECUTABLE%"
) else (
    set "PYTHON_COMMAND=python"
)

"%PYTHON_COMMAND%" "%SCRIPT_DIR%initialize_all.py" %*
set "RESULT=%ERRORLEVEL%"
endlocal & exit /b %RESULT%
