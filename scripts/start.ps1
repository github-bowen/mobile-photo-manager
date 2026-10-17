# Start Mobile Photo Manager server on Windows
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location "$ScriptDir\.."

if (Test-Path ".\.venv\Scripts\python.exe") {
    & ".\.venv\Scripts\python.exe" -m server.app
} elseif (Test-Path ".\venv\Scripts\python.exe") {
    & ".\venv\Scripts\python.exe" -m server.app
} else {
    python -m server.app
}
