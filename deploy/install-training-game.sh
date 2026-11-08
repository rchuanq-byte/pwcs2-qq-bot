#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
umask 022

die() {
  printf '训练营网页安装失败：%s\n' "$*" >&2
  exit 1
}

for command_name in cmp dirname grep install mktemp mv rm sha256sum wc; do
  command -v "$command_name" >/dev/null 2>&1 \
    || die "缺少必需命令：$command_name"
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
SOURCE="$ROOT_DIR/src/pwcs2_bot/assets/training/cs2-training.html"

[[ -n "${HOME:-}" && "$HOME" == /* ]] \
  || die 'HOME 未设置为绝对路径'
[[ -d "$HOME" ]] || die "HOME 目录不存在：$HOME"

HOME_REAL="$(cd -- "$HOME" && pwd -P)"
TEAMSPEAK_DIR="$HOME_REAL/teamspeak3"
TARGET_DIR="$TEAMSPEAK_DIR/assets"
TARGET="$TARGET_DIR/cs2-training.html"

[[ -f "$SOURCE" && ! -L "$SOURCE" ]] \
  || die "源文件不存在、不是普通文件或是符号链接：$SOURCE"
[[ -d "$TEAMSPEAK_DIR" && ! -L "$TEAMSPEAK_DIR" ]] \
  || die "TeamSpeak 目录不存在、不是目录或是符号链接：$TEAMSPEAK_DIR"
[[ -d "$TARGET_DIR" && ! -L "$TARGET_DIR" ]] \
  || die "静态资源目录不存在、不是目录或是符号链接：$TARGET_DIR"
[[ -w "$TARGET_DIR" ]] || die "静态资源目录不可写：$TARGET_DIR"

if [[ -e "$TARGET" || -L "$TARGET" ]]; then
  [[ -f "$TARGET" && ! -L "$TARGET" ]] \
    || die "目标路径已存在但不是普通文件：$TARGET"
fi

SOURCE_BYTES="$(wc -c < "$SOURCE")"
SOURCE_BYTES="${SOURCE_BYTES//[[:space:]]/}"
[[ "$SOURCE_BYTES" =~ ^[0-9]+$ ]] || die '无法读取源文件大小'
(( SOURCE_BYTES >= 1024 )) || die "源文件异常过小：${SOURCE_BYTES} 字节"
(( SOURCE_BYTES <= 2097152 )) || die "源文件超过 2 MiB 安全上限：${SOURCE_BYTES} 字节"

LC_ALL=C grep -Iq . "$SOURCE" || die '源文件为空或包含二进制数据'
grep -Fqi '<!doctype html>' "$SOURCE" || die '源文件缺少 HTML doctype'
grep -Fq '<title>CS2 战术训练营</title>' "$SOURCE" || die '源文件标题不正确'
grep -Fq '</html>' "$SOURCE" || die '源文件缺少 HTML 结束标签'

SOURCE_SHA256="$(sha256sum -- "$SOURCE")"
SOURCE_SHA256="${SOURCE_SHA256%% *}"
[[ "$SOURCE_SHA256" =~ ^[[:xdigit:]]{64}$ ]] || die '无法计算源文件 SHA-256'

TMP_PATH=''
cleanup() {
  if [[ -n "$TMP_PATH" && ( -e "$TMP_PATH" || -L "$TMP_PATH" ) ]]; then
    rm -f -- "$TMP_PATH" || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM

TMP_PATH="$(mktemp "$TARGET_DIR/.cs2-training.html.tmp.XXXXXX")" \
  || die '无法在静态资源目录创建临时文件'
install -m 0644 -- "$SOURCE" "$TMP_PATH" \
  || die '无法复制网页到临时文件'
cmp -s -- "$SOURCE" "$TMP_PATH" || die '临时文件内容校验失败'

TMP_SHA256="$(sha256sum -- "$TMP_PATH")"
TMP_SHA256="${TMP_SHA256%% *}"
[[ "$TMP_SHA256" == "$SOURCE_SHA256" ]] || die '临时文件 SHA-256 校验失败'

# The temporary file lives in the destination directory, so this rename is atomic.
mv -fT -- "$TMP_PATH" "$TARGET" || die '无法原子替换目标网页'
TMP_PATH=''

TARGET_SHA256="$(sha256sum -- "$TARGET")"
TARGET_SHA256="${TARGET_SHA256%% *}"
[[ "$TARGET_SHA256" == "$SOURCE_SHA256" ]] || die '安装后的网页 SHA-256 校验失败'

printf 'OK 训练营网页已安装：%s\n' "$TARGET"
printf 'SHA256=%s bytes=%s\n' "$SOURCE_SHA256" "$SOURCE_BYTES"
