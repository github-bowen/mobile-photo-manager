Write-Host "Stopping Mobile Photo Manager services..."

Write-Host "Freeing port 8000..."
$connections = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($connections) {
    foreach ($conn in $connections) {
        Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "Killing python processes for server..."
$pythonProcs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'python3.exe'" -ErrorAction SilentlyContinue
if ($pythonProcs) {
    foreach ($proc in $pythonProcs) {
        if ($proc.CommandLine -match "server\.app" -or $proc.CommandLine -match "server/app\.py" -or $proc.CommandLine -match "server\\app\.py") {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Host "All services have been stopped successfully."
