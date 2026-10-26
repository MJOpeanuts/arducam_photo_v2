$ErrorActionPreference = "Stop"

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python Launcher 'py' is required. Install Python 3.12 x64 from python.org."
}

py -3.12 -c "import sys; assert sys.maxsize > 2**32" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.12 must be 64-bit."
}
py -3.12 -m pip install -e ".[build]"
if ($LASTEXITCODE -ne 0) {
    throw "Unable to install the build dependencies."
}
py -3.12 -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name ArducamPhotoV2 --paths src --collect-all cv2 packaging/launcher.py
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed to produce the portable application."
}

Write-Host "Portable application: dist\ArducamPhotoV2.exe"
