#!/usr/bin/env bash
# fcitx 语音输入引擎的 ASR 服务管理
# 用法: ./service.sh [start|stop|restart]
set -e
cd "$(dirname "$0")"
PIDFILE="/tmp/voiceime-service.pid"
OSDPIDFILE="/tmp/voiceime-osd.pid"
DRAFTPIDFILE="/tmp/voiceime-draft.pid"
WEBPIDFILE="/tmp/voiceime-web.pid"

start() {
    # 日志目录（引擎/服务/浮窗日志统一放 logs/）
    mkdir -p logs
    # 录音指示浮窗（声波动画，常驻，收到 RMS 才显示；shebang 指定系统 python3.6+GTK）
    if [ ! -f "$OSDPIDFILE" ] || ! kill -0 "$(cat "$OSDPIDFILE")" 2>/dev/null; then
        setsid ./voiceime_osd.py >> logs/voiceime-osd.log 2>&1 &
        echo $! > "$OSDPIDFILE"
    fi
    # 草稿窗口（识别文本暂存，Ctrl+Alt+D 显示；常驻，窗口关闭=隐藏）
    if [ ! -f "$DRAFTPIDFILE" ] || ! kill -0 "$(cat "$DRAFTPIDFILE")" 2>/dev/null; then
        setsid ./voiceime_draft.py >> logs/voiceime-draft.log 2>&1 &
        echo $! > "$DRAFTPIDFILE"
    fi
    # 配置 Web 界面（127.0.0.1:19530，参数切换/保存/重启/日志）
    if [ ! -f "$WEBPIDFILE" ] || ! kill -0 "$(cat "$WEBPIDFILE")" 2>/dev/null; then
        setsid python3 voiceime_web.py >> logs/voiceime-web.log 2>&1 &
        echo $! > "$WEBPIDFILE"
    fi
    # ASR 服务
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "服务已在运行 (PID $(cat "$PIDFILE"))"
    else
        # 确保默认录音源正确（RDP 环境麦克风为 xrdp-source；其他环境按需改）
        # 默认源损坏时"用默认设备"的应用（会议软件等）录不到麦克风
        pactl set-default-source xrdp-source 2>/dev/null || true
        setsid python3 voice_ime_service.py >> logs/voiceime-service.log 2>&1 &
        echo $! > "$PIDFILE"
        sleep 1.5
        if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
            echo "ASR 服务已启动 (PID $(cat "$PIDFILE"))"
        else
            echo "启动失败: tail -20 logs/voiceime-service.log"
            rm -f "$PIDFILE"
            exit 1
        fi
    fi
}

stop() {
    stopped=0
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        kill "$(cat "$PIDFILE")" 2>/dev/null && stopped=1
    fi
    rm -f "$PIDFILE"
    for pid in $(pgrep -f "voice_ime_service.py" 2>/dev/null); do
        kill "$pid" 2>/dev/null && stopped=1
    done
    # 停止浮窗
    if [ -f "$OSDPIDFILE" ] && kill -0 "$(cat "$OSDPIDFILE")" 2>/dev/null; then
        kill "$(cat "$OSDPIDFILE")" 2>/dev/null && stopped=1
    fi
    rm -f "$OSDPIDFILE"
    for pid in $(pgrep -f "voiceime_osd.py" 2>/dev/null); do
        kill "$pid" 2>/dev/null && stopped=1
    done
    # 停止草稿窗口
    if [ -f "$DRAFTPIDFILE" ] && kill -0 "$(cat "$DRAFTPIDFILE")" 2>/dev/null; then
        kill "$(cat "$DRAFTPIDFILE")" 2>/dev/null && stopped=1
    fi
    rm -f "$DRAFTPIDFILE"
    for pid in $(pgrep -f "voiceime_draft.py" 2>/dev/null); do
        kill "$pid" 2>/dev/null && stopped=1
    done
    # 停止配置 Web 界面
    if [ -f "$WEBPIDFILE" ] && kill -0 "$(cat "$WEBPIDFILE")" 2>/dev/null; then
        kill "$(cat "$WEBPIDFILE")" 2>/dev/null && stopped=1
    fi
    rm -f "$WEBPIDFILE"
    for pid in $(pgrep -f "voiceime_web.py" 2>/dev/null); do
        kill "$pid" 2>/dev/null && stopped=1
    done
    [ "$stopped" = "1" ] && echo "服务已停止" || echo "服务未在运行"
}

case "${1:-start}" in
    start)
        if systemctl --user is-active --quiet voiceime-core; then
            echo "已由用户级 systemd 托管（voiceime-core/-web/-osd/-draft），请用: systemctl --user [restart|stop] voiceime-*"
        else
            start
        fi ;;
    stop)
        if systemctl --user is-active --quiet voiceime-core; then
            echo "用户级 systemd 托管中，请用: systemctl --user stop voiceime-core voiceime-web voiceime-osd voiceime-draft"
        else
            stop
        fi ;;
    restart)
        if systemctl --user is-active --quiet voiceime-core; then
            echo "用户级 systemd 托管中，请用: systemctl --user restart voiceime-core"
        else
            stop; sleep 1; start
        fi ;;
    *) echo "用法: $0 [start|stop|restart]" ;;
esac
