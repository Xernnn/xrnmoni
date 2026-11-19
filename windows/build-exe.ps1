# Build xrnmoni-agent.exe (the agent with Python inside, no console window) and a folder to
# copy to the PC that is recorded, which then needs no Python:
#
#   powershell -ExecutionPolicy Bypass -File windows\build-exe.ps1
#
# Needs Python 3.10+ on the machine that builds (here, or CI's windows job). Writes
#   windows\dist\xrnmoni-agent\          the exe and its _internal folder
#   windows\dist\xrnmoni-windows\        that, the windows scripts and the extension:
#                                        copy it to the PC and run windows\install.bat -UseExe
[CmdletBinding()]
param([string]$Python)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
$root = Split-Path $PSScriptRoot -Parent
$build = Join-Path $PSScriptRoot 'build'
$dist = Join-Path $PSScriptRoot 'dist'
$venv = Join-Path $build 'venv'
$venvPy = Join-Path $venv 'Scripts\python.exe'

if (-not $Python) {
    $r = Invoke-Native 'py.exe' @('-3', '-c', 'import sys; print(sys.executable)')
    if ($r.Code -eq 0) { $Python = ($r.Output -split "`n")[-1].Trim() }
    elseif (Get-Command python.exe -ErrorAction SilentlyContinue) { $Python = (Get-Command python.exe).Source }
    else { Write-Host 'No Python found: pass -Python C:\path\to\python.exe' -ForegroundColor Red; exit 1 }
}
Write-Host "Building with $Python"
if (-not (Test-Path $venvPy)) {
    & $Python -m venv $venv
    if ($LASTEXITCODE -ne 0) { exit 1 }
}
& $venvPy -m pip install --disable-pip-version-check --upgrade 'pyinstaller>=6.10,<7' 'pillow' `
    -r (Join-Path $root 'agent\requirements.txt')
if ($LASTEXITCODE -ne 0) { exit 1 }

& $venvPy -m PyInstaller --noconfirm --clean --distpath $dist --workpath (Join-Path $build 'work') `
    (Join-Path $PSScriptRoot 'xrnmoni-agent.spec')
if ($LASTEXITCODE -ne 0) { exit 1 }
$exe = Join-Path $dist 'xrnmoni-agent\xrnmoni-agent.exe'
if (-not (Test-Path $exe)) { Write-Host "PyInstaller made no $exe" -ForegroundColor Red; exit 1 }

$pkg = Join-Path $dist 'xrnmoni-windows'
if (Test-Path $pkg) { Remove-Item -Recurse -Force $pkg }
New-Item -ItemType Directory -Force -Path (Join-Path $pkg 'windows') | Out-Null
Copy-Item -Recurse (Join-Path $dist 'xrnmoni-agent') (Join-Path $pkg 'xrnmoni-agent')
Get-ChildItem -Path (Join-Path $PSScriptRoot '*') -File -Include '*.ps1', '*.bat', '*.md' |
    Where-Object { $_.Name -ne 'build-exe.ps1' } |
    Copy-Item -Destination (Join-Path $pkg 'windows')
Copy-Item -Recurse (Join-Path $root 'extension') (Join-Path $pkg 'extension')
Write-Host "`nBuilt $exe" -ForegroundColor Green
Write-Host "Copy $pkg to the PC and run windows\install.bat -UseExe there."
