@echo off
REM Start the Houdini MCP server (Windows). Double-click, or run from cmd.
REM
REM Must run Windows-side: it talks to Houdini's bridge on the Windows
REM loopback, which WSL cannot reach. Arguments pass through to server.main,
REM e.g. start_server.bat --port 3001 --bridge-port 8010

setlocal

if "%HOUDINI_MCP_PYTHON%"=="" (
    set "HOUDINI_MCP_PYTHON=%USERPROFILE%\.conda\envs\houdini_mcp\python.exe"
)

if not exist "%HOUDINI_MCP_PYTHON%" (
    echo [start_server] Python not found: %HOUDINI_MCP_PYTHON%
    echo [start_server] Set HOUDINI_MCP_PYTHON to the interpreter with fastmcp installed.
    exit /b 1
)

cd /d "%~dp0.."
"%HOUDINI_MCP_PYTHON%" -m server.main %*

endlocal
