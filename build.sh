#!/bin/bash
# 米家设备管家 FPK 构建脚本
# 用法: ./build.sh [版本号]   （默认取 fpk/manifest 里的 version）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="${ROOT}/app"
FPK_DIR="${ROOT}/fpk"
BUILD_DIR="${ROOT}/build"
DIST_DIR="${ROOT}/dist"
PIP="${PIP:-pip3}"

VERSION="$(grep '^version' "${FPK_DIR}/manifest" | awk '{print $3}')"
[ $# -ge 1 ] && VERSION="$1"
OUT_NAME="mihome-admin-${VERSION}-amd64.fpk"

echo "==> [1/5] 安装 Python 依赖到 app/server/vendor"
rm -rf "${APP_DIR}/server/vendor"
"${PIP}" install -q --target "${APP_DIR}/server/vendor" \
  flask pycryptodome requests
# 清理缓存与无用脚本（保留 dist-info：werkzeug 运行时要用 importlib.metadata 查版本）
find "${APP_DIR}/server/vendor" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "${APP_DIR}/server/vendor/bin" 2>/dev/null || true
find "${APP_DIR}" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

echo "==> [2/5] 打包 app.tgz"
mkdir -p "${BUILD_DIR}"
tar czf "${BUILD_DIR}/app.tgz" -C "${APP_DIR}" .

echo "==> [3/5] 写入 checksum（app.tgz md5）"
SUM="$(md5sum "${BUILD_DIR}/app.tgz" | awk '{print $1}')"
sed -E "s/^checksum[[:space:]]*=.*/checksum              = ${SUM}/" \
  "${FPK_DIR}/manifest" > "${BUILD_DIR}/manifest"
grep '^checksum' "${BUILD_DIR}/manifest"

echo "==> [4/5] 组装 FPK"
STAGE="${BUILD_DIR}/stage"
rm -rf "${STAGE}"; mkdir -p "${STAGE}"
cp "${BUILD_DIR}/app.tgz" "${STAGE}/"
cp "${BUILD_DIR}/manifest" "${STAGE}/manifest"
cp -r "${FPK_DIR}/cmd" "${FPK_DIR}/config" "${FPK_DIR}/wizard" "${STAGE}/"
cp "${FPK_DIR}/ICON.PNG" "${FPK_DIR}/ICON_256.PNG" "${STAGE}/"
mkdir -p "${DIST_DIR}"
tar czf "${DIST_DIR}/${OUT_NAME}" -C "${STAGE}" .

echo "==> [5/5] 校验产物"
tar tzf "${DIST_DIR}/${OUT_NAME}"
echo ""
echo "完成: dist/${OUT_NAME} ($(du -h "${DIST_DIR}/${OUT_NAME}" | awk '{print $1}'))"
