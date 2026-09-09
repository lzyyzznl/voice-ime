#!/usr/bin/env bash
# voice-ime 一键安装脚本（检测 → 安装依赖 → 编译引擎 → 配置 → 启动服务 → 验证）
# 用法: ./install.sh
# 依赖自动 yum 安装（sudo 免密环境可直接运行；需内网源可访问）
set -e
cd "$(dirname "$0")"

# 内网 yum 必须显式 no_proxy，否则走代理报 302
YUM="sudo -n env no_proxy=nsdlmirrors.zte.com.cn,.zte.com.cn yum install -y"

echo "voice-ime 一键安装"
echo "==================="

echo ""
echo "== 1. 依赖检查与安装 =="
MISSING=""
command -v gcc >/dev/null 2>&1          || MISSING="$MISSING gcc"
command -v make >/dev/null 2>&1         || MISSING="$MISSING make"
command -v pkg-config >/dev/null 2>&1   || MISSING="$MISSING pkg-config"
command -v python3 >/dev/null 2>&1      || MISSING="$MISSING python3"
command -v parec >/dev/null 2>&1        || MISSING="$MISSING pulseaudio-utils"
command -v paplay >/dev/null 2>&1       || MISSING="$MISSING pulseaudio-utils"
[ -f /usr/include/fcitx/fcitx.h ]       || MISSING="$MISSING fcitx-devel"
pkg-config --exists glib-2.0 2>/dev/null || MISSING="$MISSING glib2-devel"
# 录音指示浮窗：系统 python3.6 + python3-gobject + gtk3（RHEL 8 自带）
if [ ! -x /usr/bin/python3.6 ]; then
    MISSING="$MISSING python36"
elif ! /usr/bin/python3.6 -c "import gi" >/dev/null 2>&1; then
    MISSING="$MISSING python3-gobject"
fi
# 浮窗动画绘制需要 pycairo（python36-cairo）
if [ -x /usr/bin/python3.6 ] && ! /usr/bin/python3.6 -c "import cairo" >/dev/null 2>&1; then
    MISSING="$MISSING python36-cairo"
fi
pkg-config --exists gtk+-3.0 2>/dev/null  || MISSING="$MISSING gtk3"

MISSING=$(echo "$MISSING" | tr ' ' '\n' | sed '/^$/d' | sort -u | tr '\n' ' ')
if [ -n "$MISSING" ]; then
    echo "需要安装: $MISSING"
    $YUM $MISSING
    echo "依赖安装完成 ✓"
else
    echo "依赖齐全 ✓"
fi

echo ""
echo "== 2. 编译并安装引擎 =="
cd fcitx-engine
make >/dev/null
sudo -n make install
cd ..
echo "引擎已安装到 /usr/lib64/fcitx/fcitx-voiceime.so ✓"

echo ""
echo "== 3. 图标 =="
if /usr/bin/python3.6 icons/gen_icons.py --install >/dev/null; then
    echo "图标已生成并安装（imicon + fcitx-voiceime.svg 主题图标）✓"
else
    echo "  [✗] 图标安装失败（检查 python36-cairo / sudo 免密）"
fi

echo ""
echo "== 4. 配置 =="
if [ -f config.json ]; then
    echo "config.json 已存在，跳过"
else
    cp config.example.json config.json
    echo "已生成 config.json（模板）"
    echo "  >>> 请编辑 config.json 填写 ASR 凭证后重跑本脚本："
    echo "      app_id / source / user_id / secret（向 iCenter 申请）"
    echo "      凭证未填写前服务无法连接 iCenter。"
fi

echo ""
echo "== 5. 启动服务 =="
./service.sh start

echo ""
echo "== 6. 验证 =="
sleep 1.5
if pgrep -f "voice_ime_service.py" >/dev/null; then
    echo "  [✓] ASR 服务运行中"
else
    echo "  [✗] ASR 服务启动失败（日志: tail -20 logs/voiceime-service.log）"
fi
if ss -x 2>/dev/null | grep -q voiceime; then
    echo "  [✓] 引擎已连接服务"
else
    echo "  [·] 引擎未连接（正常：需切换输入法激活 voiceime 后自动连接）"
fi
if pgrep -f "voiceime_osd.py" >/dev/null; then
    echo "  [✓] 录音指示浮窗运行中"
else
    echo "  [✗] 浮窗未运行（日志: tail -20 logs/voiceime-osd.log）"
fi

echo ""
echo "======================================"
echo "安装完成。最后一步："
echo "  1. 重启 fcitx 使新引擎生效（引擎 .so 只在启动时加载）："
echo "       pkill fcitx && fcitx -d &"
echo "  2. 输入法切换到「语音输入」"
echo "  3. 按住 Ctrl+Alt ≥0.5s 开始说话，按 Escape 停止提交"
echo ""
echo "环境复查: ./check_env.sh"
