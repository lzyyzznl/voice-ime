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
        1) info "fcitx 运行中但输入法未激活（按 Ctrl+Space 激活；未激活时语音与拼音都不生效）" ;;
        2) ok "fcitx 正在运行（状态 2：已激活）" ;;
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
PY311=""
for c in "$HOME/.local/bin/python3" python3; do
    if command -v "$c" >/dev/null 2>&1; then PY311="$c"; break; fi
done
if [ -n "$PY311" ]; then
    ok "core/web 运行时: $PY311 $($PY311 --version 2>&1 | awk '{print $2}')"
    if "$PY311" -c "import websockets" >/dev/null 2>&1; then
        ok "websockets 库已装 ($($PY311 -c 'import websockets; print(websockets.__version__)' 2>/dev/null))"
    else
        bad "websockets 库缺失（$PY311 -m pip install websockets）"
    fi
else
    bad "python3 缺失（core/web 需要 Python 3.10+）"
fi
if [ -x /usr/bin/python3.6 ]; then
    ok "OSD/draft 运行时: python3.6"
    /usr/bin/python3.6 -c "import gi" >/dev/null 2>&1 && ok "python3-gobject (gi)" \
        || bad "python3-gobject 缺失（浮窗/草稿窗无法启动）"
    /usr/bin/python3.6 -c "import cairo" >/dev/null 2>&1 && ok "python36-cairo" \
        || bad "python36-cairo 缺失（浮窗绘制需要）"
else
    bad "python3.6 缺失（OSD 浮窗/草稿窗需要）"
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
    if "$PY311" -c "import json,sys; json.load(open('config.json'))" 2>/dev/null; then
        ok "config.json JSON 合法"
        DBKEY=$("$PY311" -c "import json; print((json.load(open('config.json')).get('doubao') or {}).get('api_key',''))" 2>/dev/null)
        if [ -n "$DBKEY" ] && [ "$DBKEY" != "YOUR_DOUBAO_API_KEY" ]; then
            ok "doubao.api_key 已配置"
        else
            bad "doubao.api_key 未配置（ASR 无法工作）"
        fi
        LLMACT=$("$PY311" -c "import json; print((json.load(open('config.json')).get('llm') or {}).get('active',''))" 2>/dev/null)
        [ -n "$LLMACT" ] && ok "llm.active=$LLMACT" || info "llm 未配置（润色关闭，草稿窗优化不可用）"
    else
        bad "config.json JSON 解析失败"
    fi
else
    info "config.json 缺失（运行 ./install.sh 生成模板后填写豆包凭证）"
fi

echo ""
echo "== 7. systemd 用户服务 =="
if systemctl --user list-unit-files 2>/dev/null | grep -q voiceime-core; then
    for u in core osd draft web; do
        st=$(systemctl --user is-active voiceime-$u 2>/dev/null)
        if [ "$st" = "active" ]; then
            ok "voiceime-$u active"
        else
            bad "voiceime-$u 未运行（$st）— systemctl --user start voiceime-$u"
        fi
    done
else
    bad "voiceime-*.service 未安装（运行 ./install.sh 或手动装 systemd 单元）"
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
