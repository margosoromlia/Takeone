# Сборка Takeone для Windows: папка с программой + установщик Takeone-Setup-<версия>.exe
#
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1 [-UxplayDir build-uxplay]
#
# Нужно: Python 3.10+ (с tkinter), Inno Setup 6. UxPlay (для iPhone по AirPlay) собирается
# отдельно скриптом bundle_uxplay.sh в MSYS2; без него сборка тоже пройдёт, но без режима AirPlay.
param([string]$UxplayDir = "")
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Root    = Split-Path -Parent $PSScriptRoot
$Build   = Join-Path $Root "build"
$Bin     = Join-Path $Build "bin"
$Scrcpy  = "v5.0.1"
$Version = (Select-String -Path "$Root\app\takeone.py" -Pattern '^VERSION = "(.+)"').Matches[0].Groups[1].Value
Write-Host "== Takeone $Version"

if (Test-Path $Build) { Remove-Item -Recurse -Force $Build }
New-Item -ItemType Directory -Force $Bin | Out-Null

Write-Host "== Python-зависимости"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet pyinstaller imageio-ffmpeg pillow av numpy sounddevice

Write-Host "== scrcpy $Scrcpy (с adb)"
$zip = Join-Path $Build "scrcpy.zip"
Invoke-WebRequest "https://github.com/Genymobile/scrcpy/releases/download/$Scrcpy/scrcpy-win64-$Scrcpy.zip" -OutFile $zip
Expand-Archive $zip (Join-Path $Build "scrcpy_tmp")
Move-Item (Join-Path $Build "scrcpy_tmp\scrcpy-win64-$Scrcpy") (Join-Path $Bin "scrcpy")

Write-Host "== ffmpeg"
$ff = python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
New-Item -ItemType Directory -Force (Join-Path $Bin "ffmpeg") | Out-Null
Copy-Item $ff (Join-Path $Bin "ffmpeg\ffmpeg.exe")

if ($UxplayDir -and (Test-Path (Join-Path $UxplayDir "uxplay.exe"))) {
    Write-Host "== UxPlay из $UxplayDir"
    Copy-Item -Recurse $UxplayDir (Join-Path $Bin "uxplay")
} else {
    Write-Warning "UxPlay не передан — в этой сборке не будет режима iPhone (AirPlay)."
}

Write-Host "== Иконка"
python "$Root\packaging\make_icon.py" $Build

Write-Host "== PyInstaller"
python -m PyInstaller --noconfirm --clean --windowed --name Takeone `
    --icon "$Build\icon.ico" `
    --distpath "$Build\dist" --workpath "$Build\work" --specpath $Build `
    --add-data "${Bin};bin" `
    --paths "$Root\app" --hidden-import android_view --hidden-import ui_kit --collect-submodules av `
    "$Root\app\takeone.py"

Write-Host "== Установщик"
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Не найден Inno Setup 6 (https://jrsoftware.org/isdl.php или: winget install JRSoftware.InnoSetup)" }
& $iscc "/DAppVersion=$Version" "/DBuildDir=$Build" "$Root\packaging\installer.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup завершился с ошибкой" }

Write-Host "== Готово: $Build\installer\Takeone-Setup-$Version.exe"
