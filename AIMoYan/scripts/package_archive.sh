#!/bin/bash
# 把一个 .xcarchive 里的 App 打成未签名 ipa，放到「文稿/AIMoYan-IPA」。
# 用法：package_archive.sh <xxx.xcarchive>
# 也会被 Xcode 的 Archive 后置动作自动调用（参数是 $ARCHIVE_PATH）。
set -euo pipefail

ARCHIVE="${1:?请传入 .xcarchive 路径}"
OUT_DIR="${AIMOYAN_IPA_DIR:-$HOME/Documents/AIMoYan-IPA}"

APP="$(find "$ARCHIVE/Products/Applications" -maxdepth 1 -name '*.app' -print -quit)"
if [ -z "$APP" ]; then
  echo "❌ 在 $ARCHIVE 里没找到 .app" >&2
  exit 1
fi

PLIST="$APP/Info.plist"
NAME="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$PLIST")"
VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$PLIST" 2>/dev/null || echo 1.0)"
BUILD="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$PLIST" 2>/dev/null || echo 1)"
STAMP="$(date +%Y%m%d-%H%M)"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/Payload"
cp -R "$APP" "$STAGE/Payload/"
# 去掉开发签名残留，让全能签干净地重签
rm -rf "$STAGE/Payload/$(basename "$APP")/_CodeSignature" "$STAGE/Payload/$(basename "$APP")/embedded.mobileprovision"

mkdir -p "$OUT_DIR"
IPA="$OUT_DIR/${NAME}-v${VERSION}(${BUILD})-${STAMP}.ipa"
(cd "$STAGE" && /usr/bin/zip -qry "$IPA" Payload)

echo "✅ 已生成：$IPA"
# 弹出 Finder 方便直接隔空投送到手机
open -R "$IPA" || true
