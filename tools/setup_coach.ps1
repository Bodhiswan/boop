$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskRuntime = Join-Path $taskRoot '.runtime'
$taskOllama = Join-Path $taskRuntime 'ollama'
$taskZip = Join-Path $taskRuntime 'ollama-windows-amd64.zip'
New-Item -ItemType Directory -Path $taskRuntime -Force | Out-Null
$taskExe = Join-Path $taskOllama 'ollama.exe'
if (-not (Test-Path -LiteralPath $taskExe)) {
    $taskRelease = Invoke-RestMethod 'https://api.github.com/repos/ollama/ollama/releases/latest'
    $taskAsset = $taskRelease.assets | Where-Object name -EQ 'ollama-windows-amd64.zip'
    if (-not $taskAsset.digest.StartsWith('sha256:')) { throw 'Release is missing a SHA-256 digest.' }
    $taskSource = [Uri]$taskAsset.browser_download_url
    if ($taskSource.Host -ne 'github.com' -or -not $taskSource.AbsolutePath.StartsWith('/ollama/ollama/releases/download/')) { throw 'Unexpected Ollama download source.' }
    Write-Output ('Downloading official Ollama CLI ' + $taskRelease.tag_name)
    Invoke-WebRequest -Uri $taskSource.AbsoluteUri -OutFile $taskZip
    $taskHash = (Get-FileHash -LiteralPath $taskZip -Algorithm SHA256).Hash.ToLower()
    if ($taskHash -ne $taskAsset.digest.Substring(7)) { throw 'Ollama archive checksum mismatch.' }
    Write-Output 'Official release checksum verified.'
    Expand-Archive -LiteralPath $taskZip -DestinationPath $taskOllama -Force
}
$env:OLLAMA_HOST = '127.0.0.1:11434'
$env:OLLAMA_MODELS = Join-Path $taskRuntime 'models'
$env:OLLAMA_NO_CLOUD = '1'
$taskServing = $false
try { $null = Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2; $taskServing = $true } catch {}
if (-not $taskServing) {
    Start-Process -FilePath $taskExe -ArgumentList 'serve' -WorkingDirectory $taskOllama -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskRuntime 'ollama.stdout.log') -RedirectStandardError (Join-Path $taskRuntime 'ollama.stderr.log')
    for ($taskTry = 0; $taskTry -lt 20; $taskTry++) {
        Start-Sleep -Milliseconds 500
        try { $null = Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 1; $taskServing = $true; break } catch {}
    }
}
if (-not $taskServing) { throw 'Local Ollama API did not start.' }
Write-Output 'Local AI CLI/API available at 127.0.0.1:11434. Downloading qwen3:4b-instruct weights.'
$taskModel = Invoke-RestMethod 'http://127.0.0.1:11434/api/pull' -Method Post -ContentType 'application/json' -Body '{"model":"qwen3:4b-instruct","stream":false}' -TimeoutSec 1800
$taskModel | ConvertTo-Json -Compress
Write-Output 'Offline Coach model ready.'
