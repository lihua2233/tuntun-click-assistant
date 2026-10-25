$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python -m pip install -r requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
python -m PyInstaller --noconfirm --onefile --windowed --name '豚豚定时点击助手 v1.3' --add-data 'digits.npz;.' --add-data 'calibration.npz;.' --add-data 'recording_ocr.ps1;.' click_assistant.py
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
