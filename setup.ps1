$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
$taskShell = New-Object -ComObject WScript.Shell
$taskDesktop = [Environment]::GetFolderPath('Desktop')
$taskShortcut = $taskShell.CreateShortcut((Join-Path $taskDesktop 'BOOP.lnk'))
$taskShortcut.TargetPath = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
$taskShortcut.Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + (Join-Path $PSScriptRoot 'start.ps1') + '"'
$taskShortcut.WorkingDirectory = $PSScriptRoot
$taskShortcut.Description = 'Use your BOOP strap on this laptop. Data stays local.'
$taskShortcut.Save()
Write-Output 'BOOP is installed. Open BOOP on your desktop.'
