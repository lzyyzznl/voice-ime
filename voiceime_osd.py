#!/usr/bin/python3.6
"""voice-ime 录音指示浮窗：声波动画，随麦克风音量跳动（屏幕中央偏上）。

监听 UDP 127.0.0.1:19527（voice_ime_service.py 发送）：
  {"rms": 123}      录音中，更新声波高度
  {"type": "stop"}  停止录音，隐藏浮窗
  {"type": "quit"}  退出进程
持续无数据 800ms 自动隐藏（防 stop 漏发导致的残留）。

依赖系统 python3-gobject + GTK3（RHEL 8 自带），cairo 绘制圆角半透明卡片。
Tk 9.0 无像素级透明（-transparentcolor 被移除），故用 GTK RGBA 视觉。
"""
import json
import math
import os
import socket
import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib   # noqa: E402
import cairo                          # pycairo（gi 的 cairo 封装无 OPERATOR_SOURCE）

UDP_ADDR = ("127.0.0.1", 19527)
# 可配置项（config.json 的 osd 段覆盖）：宽度/高度/垂直位置（屏幕高度百分比，水平始终居中）
DEFAULT_W, DEFAULT_H = 180, 38
DEFAULT_Y_RATIO = 0.80
CARD_W, CARD_H = DEFAULT_W, DEFAULT_H
BG = (0.06, 0.07, 0.09, 0.30)         # 卡片底色（半透明深灰黑，可透见背后文字）
FG = (0.31, 0.82, 1.0, 1.0)           # 声波条/描边/外发光青蓝
BAR_N = 9
BAR_W = 6
BAR_GAP = 12
BAR_MAX_H = 24
BAR_MIN_H = 5
TIMEOUT_MS = 800                      # 无数据自动隐藏
SMOOTH = 0.28                         # RMS 平滑系数（避免声波剧烈抖动）


def load_cfg():
    """读 config.json 的 osd 段（width/height/y_ratio），缺失/异常用默认值"""
    w, h, y = DEFAULT_W, DEFAULT_H, DEFAULT_Y_RATIO
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        with open(path, encoding="utf-8") as f:
            osd = json.load(f).get("osd") or {}
        w = int(osd.get("width", w))
        h = int(osd.get("height", h))
        y = float(osd.get("y_ratio", y))
    except Exception:
        pass
    return w, h, max(0.0, min(1.0, y))


def rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.arc(x + w - r, y + r, r, 1.5 * math.pi, 2 * math.pi)
    cr.arc(x + w - r, y + h - r, r, 0, 0.5 * math.pi)
    cr.arc(x + r, y + h - r, r, 0.5 * math.pi, math.pi)
    cr.close_path()


class OSD:
    def __init__(self):
        self.w, self.h, self.y_ratio = load_cfg()
        self.win = Gtk.Window(type=Gtk.WindowType.POPUP)   # 无装饰、不抢焦点
        self.win.set_decorated(False)
        self.win.set_keep_above(True)
        self.win.set_accept_focus(False)
        self.win.set_app_paintable(True)
        screen = self.win.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            self.win.set_visual(visual)
        self.win.set_default_size(self.w, self.h)
        self.da = Gtk.DrawingArea()
        self.da.connect("draw", self.on_draw)
        self.win.add(self.da)
        self.reposition()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(UDP_ADDR)
        self.sock.setblocking(False)
        self.target_rms = 0
        self.display_rms = 0
        self.last_data = 0.0
        self.visible = False
        self.have_rms = False   # 收到过 rms 才启用超时隐藏（防 show→首 rms 空窗闪烁）
        self.mode_b = False     # 录音模式：False=持续输入(A) 声波青蓝 / True=即停(B) 声波橙色
        self.polish_state = None   # LLM 润色状态：None=空闲 / 'on'=润色中 / 'fail'=润色失败（短暂红后恢复）
        self.polish_until = 0.0    # fail 状态的自动恢复时刻（monotonic）
        GLib.timeout_add(40, self.tick)

    def reposition(self):
        """位置：垂直按 y_ratio（屏幕高度百分比），水平始终居中。
        每次显示前调用，RDP 分辨率变化后浮窗仍落在屏内。"""
        screen = self.win.get_screen()
        geo = screen.get_monitor_geometry(screen.get_primary_monitor())
        x = geo.x + (geo.width - self.w) // 2
        y = geo.y + int(geo.height * self.y_ratio)
        self.win.move(x, y)

    def tick(self):
        while True:
            try:
                data, _ = self.sock.recvfrom(4096)
            except (BlockingIOError, OSError):
                break
            try:
                msg = json.loads(data.decode("utf-8", "replace"))
            except Exception:
                continue
            if msg.get("type") == "show":
                # 长按触发即显示：浮窗出现 = 可开始说话（服务端已连接）
                self.last_data = time.monotonic()
                self.show()
            elif msg.get("type") == "stop":
                self.hide()
            elif msg.get("type") == "mode":
                self.mode_b = bool(msg.get("b", self.mode_b))
                self.last_data = time.monotonic()
                if self.visible:
                    self.da.queue_draw()
            elif msg.get("type") == "polish":
                st = msg.get("state")
                self.polish_state = st if st in ("on", "fail") else None
                if st == "fail":
                    self.polish_until = time.monotonic() + 1.5   # 红色显示 1.5s 后自动恢复
                self.last_data = time.monotonic()
                if self.visible:
                    self.da.queue_draw()
            elif msg.get("type") == "quit":
                Gtk.main_quit()
                return False
            elif "rms" in msg:
                self.have_rms = True
                self.target_rms = max(0, int(msg["rms"]))
                self.last_data = time.monotonic()
                self.show()
        if (self.visible and self.have_rms
                and time.monotonic() - self.last_data > TIMEOUT_MS / 1000):
            self.hide()                               # 超时无数据 → 隐藏
        if self.polish_state == "fail" and time.monotonic() > self.polish_until:
            self.polish_state = None                  # 失败红 1.5s 后自动恢复默认色
        if self.visible:
            self.display_rms += (self.target_rms - self.display_rms) * SMOOTH
            self.da.queue_draw()
        return True

    def show(self):
        if not self.visible:
            self.reposition()                         # 按当前屏幕几何重新定位
            self.visible = True
            self.win.show_all()
            self.win.present()                        # 置顶显示（不抢焦点）

    def hide(self):
        self.visible = False
        self.have_rms = False
        self.target_rms = 0
        self.display_rms = 0
        self.polish_state = None
        self.win.hide()

    def on_draw(self, widget, cr):
        # 声波条只表示录音模式：青蓝=持续输入 A，橙=即停 B（松手即上屏）
        # 润色状态改用卡片描边提示，不改变声波主色：黄=润色中，红=失败（1.5 秒后恢复）
        bar_color = (1.0, 0.62, 0.25, 1.0) if self.mode_b else FG
        if self.polish_state == "fail":
            edge_color = (1.0, 0.25, 0.25, 0.95)
        elif self.polish_state == "on":
            edge_color = (1.0, 0.85, 0.25, 0.95)
        else:
            edge_color = bar_color
        aw, ah = widget.get_allocated_width(), widget.get_allocated_height()
        if aw < 2 or ah < 2:
            aw, ah = self.w, self.h
        # 清透明背景（RGBA 视觉下 alpha=0）
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        # 高亮阴影（三层，保证任意背景下可见）：
        # 1. 黑色投影（向右下偏移）→ 浅色背景下的轮廓（深色背景不可见）
        rounded_rect(cr, 4.5, 5, aw - 9, ah - 9, 16)
        cr.set_source_rgba(0, 0, 0, 0.28)
        cr.fill()
        # 2. 外发光（外扩半透明描边）→ 深色背景下的光晕，跟随当前状态色
        rounded_rect(cr, 0.5, 0.5, aw - 1, ah - 1, 19)
        cr.set_source_rgba(edge_color[0], edge_color[1], edge_color[2], 0.30)
        cr.set_line_width(3.0)
        cr.stroke()
        # 3. 圆角半透明卡片
        rounded_rect(cr, 3, 3, aw - 6, ah - 6, 16)
        cr.set_source_rgba(*BG)
        cr.fill()
        # 圆角卡片描边：润色中变黄、失败变红，其余跟随模式色
        rounded_rect(cr, 3.5, 3.5, aw - 7, ah - 7, 15)
        cr.set_source_rgba(*edge_color)
        cr.set_line_width(1.2)
        cr.stroke()
        # 声波条：高度随 RMS，轻微相位差 → 波纹效果
        rms = self.display_rms
        base = BAR_MIN_H + min(rms / 300.0, 1.0) * (BAR_MAX_H - BAR_MIN_H) * 0.75
        cy = ah / 2
        t = time.monotonic() * 4.0
        bar_area_w = BAR_N * BAR_W + (BAR_N - 1) * BAR_GAP
        x0 = (aw - bar_area_w) / 2
        bars = []
        for i in range(BAR_N):
            wave = 0.6 + 0.4 * math.sin(t + i * 0.9)
            bars.append((x0 + i * (BAR_W + BAR_GAP), max(BAR_MIN_H, base * wave)))
        # 深色阴影（浅色背景下声波条轮廓；深色背景下不可见）
        cr.set_source_rgba(0, 0, 0, 0.30)
        for bx, h in bars:
            rounded_rect(cr, bx + 1.5, cy - h / 2 + 1.5, BAR_W, h, BAR_W / 2)
            cr.fill()
        cr.set_source_rgba(*bar_color)
        for bx, h in bars:
            rounded_rect(cr, bx, cy - h / 2, BAR_W, h, BAR_W / 2)
            cr.fill()


def main():
    OSD()
    Gtk.main()


if __name__ == "__main__":
    main()
