@echo off
rem claude-workflow shim -- calls the CLI without requiring `pip install` / PATH-installed entry-point.
rem Safe to call as `workflow.cmd <args>` from any cwd (uses absolute path to this file's dir).
rem In Windows PowerShell 5.1 `workflow` alone is a reserved keyword (PSWorkflow);
rem always call this shim by its full name `workflow.cmd` or via the call operator `& workflow`.
setlocal
set "REPO=%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%REPO%cli\agent.py" %*
  exit /b %errorlevel%
)
python "%REPO%cli\agent.py" %*
exit /b %errorlevel%
