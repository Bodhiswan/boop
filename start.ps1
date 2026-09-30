param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskUrl = 'http://127.0.0.1:8765'
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$taskHealthy = $false
$taskOllama = Join-Path $PSScriptRoot '.runtime\ollama\ollama.exe'
if (Test-Path -LiteralPath $taskOllama) {
    $taskModelHealthy = $false
    try { $null = Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2; $taskModelHealthy = $true } catch {}
    if (-not $taskModelHealthy) {
        $env:OLLAMA_HOST = '127.0.0.1:11434'
        $env:OLLAMA_MODELS = Join-Path $PSScriptRoot '.runtime\models'
        $env:OLLAMA_NO_CLOUD = '1'
        Start-Process -FilePath $taskOllama -ArgumentList 'serve' -WindowStyle Hidden -WorkingDirectory $PSScriptRoot
    }
}
try {
    $taskStatus = Invoke-RestMethod "$taskUrl/api/status" -TimeoutSec 2
    $taskHealthy = $null -ne $taskStatus.store.database
} catch {}
if (-not $taskHealthy) {
    if (-not (Test-Path -LiteralPath $taskPython)) {
        throw 'The BOOP Python environment is missing. Run setup.ps1 to restore it.'
    }
    $taskData = Join-Path $PSScriptRoot 'data'
    New-Item -ItemType Directory -Path $taskData -Force | Out-Null
    Start-Process -FilePath $taskPython -ArgumentList @('-u','boop.py') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskData 'server.stdout.log') -RedirectStandardError (Join-Path $taskData 'server.stderr.log')
    for ($taskTry = 0; $taskTry -lt 30; $taskTry++) {
        Start-Sleep -Milliseconds 300
        try {
            $taskStatus = Invoke-RestMethod "$taskUrl/api/status" -TimeoutSec 1
            if ($null -ne $taskStatus.store.database) { $taskHealthy = $true; break }
        } catch {}
    }
    if (-not $taskHealthy) { throw 'BOOP did not start. Check data/server.stderr.log.' }
}
if (-not $NoBrowser) { Start-Process $taskUrl }
