#!/usr/bin/env bash
# voice-ime 环境检测脚本
# 用法: ./check_env.sh
# 输出每项 [✓]/[✗]，退出码 = 缺失项数量（0 表示环境就绪）
cd "$(dirname "$0")"

PASS=0; FAIL=0
ok()   { echo "  [✓] $1"; PASS=$((PASS+1)); }
bad()  { echo "  [✗] $1"; FAIL=$((FAIL+1)); }
info() { echo "  [·] $1"; }

echo "voice-ime 环境检测"
echo "==================="

echo ""
echo "== 1. 系统 =="
if [ "$(uname -s)" = "Linux" ]; then
    ok "操作系统: Linux ($(uname -m))"
else
    bad "操作系统: 非 Linux（仅支持 Linux）"
fi
if [ -n "$DISPLAY" ]; then
    ok "X11 DISPLAY=$DISPLAY"
elif [ "$XDG_SESSION_TYPE" = "wayland" ]; then
    bad "Wayland 会话（fcitx4 引擎需要 X11）"
else
    bad "未检测到 X11 显示环境（DISPLAY 为空）"
fi

echo ""
echo "== 2. 输入法（fcitx 4.x）=="
if command -v fcitx-remote >/dev/null 2>&1; then
    ok "fcitx-remote 存在"
    state=$(fcitx-remote 2>/dev/null)
    case "$state" in
        0) info "fcitx 未运行（安装后需启动 fcitx）" ;;
        1|2) ok "fcitx 正在运行（状态 $state）" ;;
        *) info "fcitx 状态未知（fcitx-remote 返回: $state）" ;;
    esac
else
    bad "fcitx-remote 不存在（需先安装并启用 fcitx 4.x）"
fi
if rpm -q fcitx 2>/dev/null | grep -qE "fcitx-4"; then
    ok "fcitx 版本: $(rpm -q fcitx 2>/dev/null | head -1)"
fi

echo ""
echo "== 3. 编译工具 =="
for c in gcc make pkg-config; do
    command -v "$c" >/dev/null 2>&1 && ok "$c" || bad "$c 缺失"
done
[ -f /usr/include/fcitx/fcitx.h ] && ok "fcitx-devel 头文件" \
    || bad "fcitx-devel 缺失（install.sh 会自动安装）"
pkg-config --exists glib-2.0 2>/dev/null && ok "glib2-devel" \
    || bad "glib2-devel 缺失（install.sh 会自动安装）"

echo ""
echo "== 4. 运行时 =="
if command -v python3 >/dev/null 2>&1; then
    ok "python3 $(python3 --version 2>&1 | awk '{print $2}')"
else
    bad "python3 缺失（install.sh 会自动安装）"
fi
for c in parec paplay; do
    command -v "$c" >/dev/null 2>&1 && ok "$c" \
        || bad "$c 缺失（install.sh 会自动安装 pulseaudio-utils）"
done

echo ""
echo "== 5. 麦克风 =="
if command -v pactl >/dev/null 2>&1; then
    n=$(pactl list sources short 2>/dev/null | wc -l)
    if [ "$n" -gt 0 ]; then
        ok "检测到 $n 个音频源"
    else
        bad "无可用音频源（PulseAudio 未运行或没有麦克风设备）"
    fi
else
    info "pactl 缺失，跳过音频源检测"
fi

echo ""
echo "== 6. 配置 =="
if [ -f config.json ]; then
    ok "config.json 存在"
    if ! python3 -c "import json; json.load(open('config.json'))" 2>/dev/null; then
        bad "config.json 格式无效（JSON 解析失败）"
    fi
else
    info "config.json 缺失（运行 ./install.sh 生成模板后填写 ASR 凭证）"
fi

echo ""
echo "======================================"
echo "结果: $PASS 项通过, $FAIL 项缺失"
if [ "$FAIL" -eq 0 ]; then
    echo "环境就绪，可直接使用 ✓"
else
    echo "存在缺失，运行 ./install.sh 自动安装补齐"
fi
exit "$FAIL"
