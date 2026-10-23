$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python -m pip install -r requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
python -m PyInstaller --noconfirm --onefile --windowed --name '豚豚定时点击助手(MinDna开发 v1.0)' --add-data 'digits.npz;.' click_assistant.py
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
Write-Host 'Build complete. The executable is in dist/.'
