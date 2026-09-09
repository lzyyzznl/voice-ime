#!/usr/bin/env bash
# voice-ime 一键安装：依赖 → 引擎 → 图标 → systemd 用户单元 → 启动 → 验证
# 用法: ./install.sh [代码目录]
#   - 默认安装到 ~/.config/systemd/user/voiceime-*.service（路径 = 本目录）
#   - 已存在 config.json 则保留；否则从 config.example.json 复制
#   - 已有旧 PID 方式服务（service.sh 旧版）先停掉避免端口冲突
set -e
cd "$(dirname "$0")"
APP_DIR="$(pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

# 内网 yum 必须显式 no_proxy，否则走代理报 302
YUM="sudo -n env no_proxy=nsdlmirrors.zte.com.cn,.zte.com.cn yum install -y"

echo "voice-ime 一键安装"
echo "==================="
echo "代码目录: $APP_DIR"
echo "systemd : $UNIT_DIR"

echo ""
echo "== 1. 依赖检查与安装 =="
MISSING=""
command -v gcc >/dev/null 2>&1          || MISSING="$MISSING gcc"
command -v make >/dev/null 2>&1         || MISSING="$MISSING make"
command -v pkg-config >/dev/null 2>&1   || MISSING="$MISSING pkg-config"
command -v parec >/dev/null 2>&1        || MISSING="$MISSING pulseaudio-utils"
command -v paplay >/dev/null 2>&1       || MISSING="$MISSING pulseaudio-utils"
[ -f /usr/include/fcitx/fcitx.h ]       || MISSING="$MISSING fcitx-devel"
pkg-config --exists glib-2.0 2>/dev/null || MISSING="$MISSING glib2-devel"
pkg-config --exists gtk+-3.0 2>/dev/null || MISSING="$MISSING gtk3"
# core/web 用 python3.11（本仓库环境）；找不到时降级探测 python3
PY311=""
for c in "$HOME/.local/bin/python3" python3; do
    if command -v "$c" >/dev/null 2>&1; then
        PY311="$c"; break
    fi
done
[ -n "$PY311" ] || MISSING="$MISSING python3(>=3.10)"
# core 依赖 websockets 库
if [ -n "$PY311" ] && ! "$PY311" -c "import websockets" >/dev/null 2>&1; then
    echo "  [!] $PY311 缺 websockets 库: pip install websockets"
    MISSING="$MISSING websockets"
fi
# OSD/draft 用系统 python3.6 + GTK3
if [ ! -x /usr/bin/python3.6 ]; then
    MISSING="$MISSING python3.6"
else
    /usr/bin/python3.6 -c "import gi" >/dev/null 2>&1 || MISSING="$MISSING python3-gobject"
    /usr/bin/python3.6 -c "import cairo" >/dev/null 2>&1 || MISSING="$MISSING python36-cairo"
fi

MISSING=$(echo "$MISSING" | tr ' ' '\n' | sed '/^$/d' | sort -u | tr '\n' ' ')
if [ -n "$MISSING" ]; then
    echo "缺失:$MISSING"
    # 常见项直接 yum（no_proxy 规避 302）
    PKGS=""
    for x in $MISSING; do
        case "$x" in
            gcc) PKGS="$PKGS gcc";;
            make) PKGS="$PKGS make";;
            pkg-config) PKGS="$PKGS pkgconf";;
            pulseaudio-utils) PKGS="$PKGS pulseaudio-utils";;
            fcitx-devel) PKGS="$PKGS fcitx-devel";;
            glib2-devel) PKGS="$PKGS glib2-devel";;
            gtk3) PKGS="$PKGS gtk3";;
            python3-gobject) PKGS="$PKGS python3-gobject";;
            python36-cairo) PKGS="$PKGS python36-cairo";;
        esac
    done
    if [ -n "$PKGS" ]; then
        echo "尝试安装: $PKGS"
        $YUM $PKGS || echo "  yum 失败，请手动安装后重跑"
    fi
    echo "仍缺: $MISSING（python/websockets 需手动）——继续尝试，引擎/systemd 部分可先装"
else
    echo "依赖齐全 ✓"
fi

echo ""
echo "== 2. 编译并安装引擎 =="
(cd fcitx-engine && make >/dev/null && sudo -n make install)
echo "引擎已安装到 /usr/lib64/fcitx/fcitx-voiceime.so ✓"

echo ""
echo "== 3. 图标 =="
if /usr/bin/python3.6 icons/gen_icons.py --install >/dev/null 2>&1; then
    echo "图标已安装 ✓"
else
    echo "图标安装失败（忽略，不影响核心功能）"
fi

echo ""
echo "== 4. 配置 =="
if [ -f config.json ]; then
    echo "config.json 已存在，保留（含凭证）"
else
    cp config.example.json config.json
    echo "已从 config.example.json 生成 config.json —— 记得填豆包 api_key 与 llm key！"
fi

echo ""
echo "== 5. 停旧服务（PID 方式残留）=="
pkill -f "voice_ime_service.py" 2>/dev/null && echo "旧 core 已停" || true
pkill -f "voiceime_osd.py" 2>/dev/null && echo "旧 osd 已停" || true
pkill -f "voiceime_draft.py" 2>/dev/null && echo "旧 draft 已停" || true
pkill -f "voiceime_web.py" 2>/dev/null && echo "旧 web 已停" || true

echo ""
echo "== 6. 安装 systemd 用户单元 =="
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/voiceime-core.service" <<EOF
[Unit]
Description=voice-ime ASR core service (fcitx4 voice input, user session)
After=sound.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=DISPLAY=${DISPLAY:-:0}
Environment=PULSE_SERVER=unix:/run/user/$(id -u)/pulse/native
ExecStart=$PY311 voice_ime_service.py
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF
cat > "$UNIT_DIR/voiceime-osd.service" <<EOF
[Unit]
Description=voice-ime recording indicator OSD (GTK, user session)

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=DISPLAY=${DISPLAY:-:0}
Environment=PULSE_SERVER=unix:/run/user/$(id -u)/pulse/native
ExecStart=/usr/bin/python3.6 ./voiceime_osd.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
cat > "$UNIT_DIR/voiceime-draft.service" <<EOF
[Unit]
Description=voice-ime draft editor window (GTK, user session)

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=DISPLAY=${DISPLAY:-:0}
Environment=PULSE_SERVER=unix:/run/user/$(id -u)/pulse/native
ExecStart=/usr/bin/python3.6 ./voiceime_draft.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
cat > "$UNIT_DIR/voiceime-web.service" <<EOF
[Unit]
Description=voice-ime config web console (127.0.0.1:19530, user session)

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$PY311 voiceime_web.py
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable voiceime-core voiceime-osd voiceime-draft voiceime-web >/dev/null 2>&1 || true
echo "4 个 systemd 单元已写入并 enable ✓"

echo ""
echo "== 7. 启动并验证 =="
systemctl --user restart voiceime-core voiceime-osd voiceime-draft voiceime-web
sleep 2
ACTIVE=$(systemctl --user is-active voiceime-core voiceime-osd voiceime-draft voiceime-web | tr '\n' ' ')
echo "服务状态: $ACTIVE"

echo ""
echo "======================================"
echo "安装完成。最后一步："
echo "  1. 重启 fcitx 使新引擎生效（.so 只在启动时加载）："
echo "       pkill fcitx && sleep 2"
echo "       export DISPLAY=${DISPLAY:-:0} DBUS_SESSION_BUS_ADDRESS=\"unix:path=/run/user/$(id -u)/bus\""
echo "       setsid fcitx -d"
echo "  2. 填好 config.json 凭证（豆包 doubao.api_key / llm models）"
echo "  3. 输入法切换到「语音输入」，长按 Ctrl+Alt 说话"
echo ""
echo "环境复查: ./check_env.sh"
echo "服务管理: systemctl --user status voiceime-core  |  ./service.sh restart"
