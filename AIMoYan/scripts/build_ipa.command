#!/bin/bash
# 双击运行：不需要证书和设备名额，直接编译出未签名 ipa，放到「文稿/AIMoYan-IPA」，
# 再隔空投送到手机，用「全能签」导入、签名、安装。
set -euo pipefail
cd "$(dirname "$0")/.."            # 项目根目录（AIMoYan.xcodeproj 所在处）

ARCHIVE="build/AIMoYan.xcarchive"
rm -rf "$ARCHIVE"
echo "▶ 正在编译 Release（第一次会慢一些）…"
xcodebuild -project AIMoYan.xcodeproj -scheme AIMoYan -configuration Release \
  -destination 'generic/platform=iOS' -archivePath "$ARCHIVE" archive -quiet \
  CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY="" DEVELOPMENT_TEAM=""

"$(dirname "$0")/package_archive.sh" "$ARCHIVE"
echo
echo "完成。把 ipa 隔空投送到 iPhone → 用全能签打开 → 签名 → 安装。"
read -n 1 -s -r -p "按任意键关闭…"
