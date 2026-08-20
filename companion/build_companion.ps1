$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Out = 'H:\RadioTEDU-Services-Portable'
$Python = 'C:\Users\tedu\AppData\Local\Programs\Python\Python312\python.exe'

New-Item -ItemType Directory -Force -Path $Out | Out-Null
& $Python -m PyInstaller --noconfirm --clean --onefile --windowed --name 'RadioTEDU-Services' --distpath $Out --workpath "$Here\build" --specpath "$Here\build" "$Here\services_companion.py"
Copy-Item "$Here\connections.json" "$Out\connections.json" -Force
Copy-Item "$Here\README.txt" "$Out\README.txt" -Force
Copy-Item "$Here\Install-On-Another-PC.ps1" "$Out\Install-On-Another-PC.ps1" -Force
Write-Host "Built: $Out\RadioTEDU-Services.exe"
