# Builds dist\Repro.exe: one portable file, nothing to install on the target machine.
# -Folder builds dist\Repro\ instead: a portable folder that starts much faster.
param([switch]$Folder)
$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    py -3.12 -m venv (Join-Path $PSScriptRoot ".venv")
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
}
& $python -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot "requirements.txt") "pyinstaller==6.22.3"
if ($LASTEXITCODE) { exit $LASTEXITCODE }
$specArgs = if ($Folder) { @("--", "--folder") } else { @() }
& $python -m PyInstaller --noconfirm --clean `
    --distpath (Join-Path $PSScriptRoot "dist") `
    --workpath (Join-Path $PSScriptRoot "build") `
    (Join-Path $PSScriptRoot "Repro.spec") @specArgs
if ($LASTEXITCODE) { exit $LASTEXITCODE }
$result = Join-Path $PSScriptRoot $(if ($Folder) { "dist\Repro\Repro.exe" } else { "dist\Repro.exe" })
# Explorer keeps showing an exe's old icon from its cache until told the file changed.
& $python -c "import ctypes, sys; ctypes.windll.shell32.SHChangeNotify(0x2000, 0x5, sys.argv[1], None)" $result
Write-Host "Done: $result"
