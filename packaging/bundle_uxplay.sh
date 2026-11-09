#!/usr/bin/env bash
# Собирает UxPlay (AirPlay-приёмник) под Windows и складывает его вместе со всем
# нужным GStreamer в одну переносимую папку.
# Запускать в оболочке MSYS2 UCRT64:   bash packaging/bundle_uxplay.sh <папка-вывода>
set -euo pipefail

OUT="${1:?укажите папку вывода}"
# Версия UxPlay с фрагментированной MP4-записью (файл читается во время записи)
UXPLAY_REF="${UXPLAY_REF:-05b0275cb1b726592700e47a6e0307544b3d3a52}"
P=mingw-w64-ucrt-x86_64

pacman -S --needed --noconfirm git $P-cmake $P-gcc $P-ninja $P-pkgconf $P-openssl $P-libplist \
  $P-gstreamer $P-gst-plugins-base $P-gst-plugins-good $P-gst-plugins-bad $P-gst-libav

WORK="$(mktemp -d)"
git clone --quiet https://github.com/FDH2/UxPlay "$WORK/UxPlay"
git -C "$WORK/UxPlay" checkout --quiet "$UXPLAY_REF"
cmake -S "$WORK/UxPlay" -B "$WORK/build" -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build "$WORK/build"

rm -rf "$OUT"
mkdir -p "$OUT/lib/gstreamer-1.0" "$OUT/libexec/gstreamer-1.0"
cp "$WORK/build/uxplay.exe" "$OUT/"
cp /ucrt64/lib/gstreamer-1.0/*.dll "$OUT/lib/gstreamer-1.0/"
cp /ucrt64/libexec/gstreamer-1.0/gst-plugin-scanner.exe "$OUT/libexec/gstreamer-1.0/"

# Все DLL из /ucrt64, от которых зависят uxplay.exe, плагины и сканер, — рядом с uxplay.exe.
# ldd показывает и зависимости зависимостей, поэтому одного прохода достаточно.
ldd "$OUT/uxplay.exe" "$OUT"/lib/gstreamer-1.0/*.dll "$OUT"/libexec/gstreamer-1.0/*.exe \
  | awk '{print $3}' | grep -i '^/ucrt64/' | sort -u | while read -r dll; do
    cp -n "$dll" "$OUT/"
  done

# Проверка: бинарник запускается и видит нужные плагины
export PATH="$OUT:$PATH" GST_PLUGIN_SYSTEM_PATH="$OUT/lib/gstreamer-1.0" GST_PLUGIN_PATH="$OUT/lib/gstreamer-1.0"
"$OUT/uxplay.exe" -h 2>&1 | grep -qi uxplay || { echo "uxplay.exe не запускается" >&2; exit 1; }
for el in appsrc h264parse mp4mux aacparse avdec_h264 autovideosink autoaudiosink; do
  gst-inspect-1.0 "$el" > /dev/null || { echo "нет GStreamer-элемента: $el" >&2; exit 1; }
done

echo "UxPlay собран в $OUT ($(du -sh "$OUT" | cut -f1))"
