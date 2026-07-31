#!/bin/zsh
set -euo pipefail

repo_dir=${0:A:h:h}
app_dir=${1:-$HOME/Applications}
bundle="$app_dir/Huginn.app"

mkdir -p "$bundle/Contents/MacOS" "$bundle/Contents/Resources"
cp "$repo_dir/huginn/server/static/bird.svg" "$bundle/Contents/Resources/bird.svg"
/usr/bin/swiftc \
  -O \
  -parse-as-library \
  -framework AppKit \
  -framework Foundation \
  "$repo_dir/macos/HuginnMenuBar.swift" \
  -o "$bundle/Contents/MacOS/Huginn"

/usr/libexec/PlistBuddy -c 'Clear dict' "$bundle/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c 'Add :CFBundleExecutable string Huginn' "$bundle/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :CFBundleIdentifier string is.tohuw.huginn.menubar' "$bundle/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :CFBundleName string Huginn' "$bundle/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :CFBundleDisplayName string Huginn' "$bundle/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :CFBundlePackageType string APPL' "$bundle/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :CFBundleShortVersionString string 2026.07.18' "$bundle/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :CFBundleIconFile string Huginn.icns' "$bundle/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :LSUIElement bool true' "$bundle/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :NSHighResolutionCapable bool true' "$bundle/Contents/Info.plist"
# Last-resort hint for spawning the daemon (issue #37). The app prefers a
# bundled runtime, an enclosing checkout, then daemon.json; this only matters
# when none of those exist -- e.g. the first launch of a bundle installed to
# ~/Applications, away from the checkout that built it.
/usr/libexec/PlistBuddy -c "Add :HuginnRepoPath string $repo_dir" "$bundle/Contents/Info.plist"
plutil -lint "$bundle/Contents/Info.plist"

iconset=$(mktemp -d)/Huginn.iconset
mkdir -p "$iconset"
for size in 16 32 128 256 512; do
  sips -s format png -z "$size" "$size" "$repo_dir/huginn/server/static/bird.svg" \
    --out "$iconset/icon_${size}x${size}.png" >/dev/null
  retina=$((size * 2))
  sips -s format png -z "$retina" "$retina" "$repo_dir/huginn/server/static/bird.svg" \
    --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$iconset" -o "$bundle/Contents/Resources/Huginn.icns"
codesign --force --deep --sign - "$bundle"
echo "$bundle"
