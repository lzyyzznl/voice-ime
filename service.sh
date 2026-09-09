#!/usr/bin/env bash
# voice-ime 服务快捷管理（systemd 用户服务封装）
# 用法: ./service.sh [start|stop|restart|status]
#   等价于对 voiceime-{core,osd,draft,web} 4 个单元批量操作
set -e
UNITS="voiceime-core voiceime-osd voiceime-draft voiceime-web"

case "${1:-status}" in
    start)
        systemctl --user start $UNITS
        echo "已启动: $UNITS"
        ;;
    stop)
        systemctl --user stop $UNITS
        echo "已停止"
        ;;
    restart)
        systemctl --user restart $UNITS
        sleep 1
        echo "已重启，状态:"
        systemctl --user is-active $UNITS
        ;;
    status)
        systemctl --user status $UNITS --no-pager || true
        ;;
    *)
        echo "用法: $0 [start|stop|restart|status]"
        exit 1
        ;;
esac
