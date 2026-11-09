#!/usr/bin/env bash
# Сборка Takeone для macOS: Takeone.app и образ Takeone-<версия>-macOS-<arch>.dmg
#
#   bash packaging/build_macos.sh
#
# Нужно: Python 3.10+ с tkinter (python.org или actions/setup-python), Xcode Command Line Tools.
# Собирается под архитектуру текущего Mac (Apple Silicon → arm64, Intel → x86_64).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
BIN="$BUILD/bin"
SCRCPY="v5.0.1"
ARCH="$(uname -m)"
case "$ARCH" in
  arm64) SARCH=aarch64 ;;
  x86_64) SARCH=x86_64 ;;
  *) echo "неизвестная архитектура $ARCH" >&2; exit 1 ;;
esac
VERSION="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$ROOT/app/takeone.py")"
PY="${PYTHON:-python3}"
echo "== Takeone $VERSION для macOS $ARCH"

rm -rf "$BUILD"
mkdir -p "$BIN/scrcpy" "$BIN/ffmpeg"

echo "== Python-зависимости"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet pyinstaller imageio-ffmpeg pillow
"$PY" -c "import tkinter" || { echo "у этого Python нет tkinter — поставьте Python с python.org" >&2; exit 1; }

echo "== scrcpy $SCRCPY (с adb)"
curl -fsSL "https://github.com/Genymobile/scrcpy/releases/download/$SCRCPY/scrcpy-macos-$SARCH-$SCRCPY.tar.gz" \
  | tar xz -C "$BUILD"
cp -R "$BUILD/scrcpy-macos-$SARCH-$SCRCPY/." "$BIN/scrcpy/"
rm -f "$BIN/scrcpy/scrcpy.1"

echo "== ffmpeg"
cp "$("$PY" -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())')" "$BIN/ffmpeg/ffmpeg"
chmod +x "$BIN/ffmpeg/ffmpeg" "$BIN/scrcpy/scrcpy" "$BIN/scrcpy/adb"

echo "== iOS-хелпер (запись iPhone по USB)"
swiftc -O "$ROOT/app/ios_capture.swift" -o "$BIN/ios_capture"

# Каждый исполняемый файл должен быть подписан (хотя бы ad-hoc), иначе macOS его не запустит
for f in "$BIN/scrcpy/scrcpy" "$BIN/scrcpy/adb" "$BIN/ffmpeg/ffmpeg" "$BIN/ios_capture"; do
  xattr -c "$f" 2>/dev/null || true
  codesign --force --sign - "$f"
done

echo "== Иконка"
"$PY" "$ROOT/packaging/make_icon.py" "$BUILD"

echo "== PyInstaller"
"$PY" -m PyInstaller --noconfirm --clean --windowed --name Takeone \
  --icon "$BUILD/icon.icns" --osx-bundle-identifier app.takeone \
  --distpath "$BUILD/dist" --workpath "$BUILD/work" --specpath "$BUILD" \
  --add-data "$BIN:bin" \
  "$ROOT/app/takeone.py"

APPDIR="$BUILD/dist/Takeone.app"
PLIST="$APPDIR/Contents/Info.plist"
# Без этих строк macOS не даст захватывать экран iPhone по USB (он считается «камерой»)
plutil -replace NSCameraUsageDescription -string "Takeone показывает и записывает экран iPhone/iPad, подключённого по USB." "$PLIST"
plutil -replace NSMicrophoneUsageDescription -string "Takeone записывает звук iPhone/iPad, подключённого по USB." "$PLIST"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$PLIST"
plutil -replace CFBundleVersion -string "$VERSION" "$PLIST"
plutil -replace LSMinimumSystemVersion -string "11.0" "$PLIST"
codesign --force --deep --sign - "$APPDIR"
codesign --verify --deep "$APPDIR"

echo "== DMG"
STAGE="$BUILD/dmg"
mkdir -p "$STAGE"
cp -R "$APPDIR" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
DMG="$BUILD/Takeone-$VERSION-macOS-$ARCH.dmg"
hdiutil create -volname "Takeone" -srcfolder "$STAGE" -ov -format UDZO "$DMG" > /dev/null

echo "== Готово: $DMG"
