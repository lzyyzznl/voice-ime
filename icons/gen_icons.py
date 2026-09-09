#!/usr/bin/python3.6
# -*- coding: utf-8 -*-
"""voice-ime 输入法图标生成器（搜狗式：圆角渐变方块 + 白色麦克风）

用法：
    python3.6 icons/gen_icons.py            生成 icons/voiceime.png（48x48，imicon 尺寸）
    python3.6 icons/gen_icons.py --install  生成并安装（需 sudo）：
        - fcitx imicon（voiceime.png）
        - fcitx-voiceime.svg 到 Nde 主题 Dark/Light/Fashion 的 panel/ 与 actions/
          ——fcitx SNI 托盘激活时按 IconName "fcitx-voiceime" 从主题 panel/
          加载 SVG（面板托盘只用 SVG，同搜狗 fcitx-sogouimebs 布局）
"""
import cairo
import os
import subprocess
import sys

ICON_DIR = os.path.dirname(os.path.abspath(__file__))

# 背景垂直渐变（搜狗式天蓝 → 深蓝）
IDLE_STOPS = ((0.0, 0.36, 0.74, 1.00), (1.0, 0.05, 0.30, 0.74))

# 面板图标尺寸目录（与搜狗 fcitx-sogouimebs 一致）
THEME_SIZES = (16, 22, 24, 32, 48, 64, 128, 256)


def rounded_rect(ctx, x, y, w, h, r):
    """圆角矩形路径（逆时针）"""
    r = min(r, w / 2, h / 2)
    ctx.move_to(x + r, y)
    ctx.arc(x + w - r, y + r, r, -1.5708, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, 1.5708)
    ctx.arc(x + r, y + h - r, r, 1.5708, 3.1416)
    ctx.arc(x + r, y + r, r, 3.1416, 4.7124)
    ctx.close_path()


def draw_mic(size):
    """绘制一帧图标：圆角渐变方块 + 白色麦克风（搜狗式）。"""
    S = float(size)
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    ctx = cairo.Context(surf)

    # ── 背景：圆角方块 + 垂直渐变 ──
    pad = S * 0.06
    rounded_rect(ctx, pad, pad, S - 2 * pad, S - 2 * pad, S * 0.20)
    grad = cairo.LinearGradient(0, pad, 0, S - pad)
    for pos, r, g, b in IDLE_STOPS:
        grad.add_color_stop_rgb(pos, r, g, b)
    ctx.set_source(grad)
    ctx.fill()

    cx = S * 0.5
    # ── 白色麦克风 ──
    ctx.set_source_rgb(1, 1, 1)
    hw, hh = S * 0.095, S * 0.14          # 话筒头胶囊半宽/半高
    cy = S * 0.38
    rounded_rect(ctx, cx - hw, cy - hh, 2 * hw, 2 * hh, hw)
    ctx.fill()
    # U 形支架：贝塞尔从话筒头底边两侧收拢到中心
    ctx.set_line_width(S * 0.05)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)
    ctx.move_to(cx - hw, cy + hh)
    ctx.curve_to(cx - hw, S * 0.60, cx - S * 0.06, S * 0.63, cx, S * 0.65)
    ctx.curve_to(cx + S * 0.06, S * 0.63, cx + hw, S * 0.60, cx + hw, cy + hh)
    ctx.stroke()
    # 底座横条
    bw, bh = S * 0.17, S * 0.05
    rounded_rect(ctx, cx - bw, S * 0.70 - bh / 2, 2 * bw, bh, bh / 2)
    ctx.fill()
    return surf


def write_png(surf, path):
    surf.write_to_png(path)
    print("已生成 %s (%dx%d)" % (path, surf.get_width(), surf.get_height()))


def main():
    install = "--install" in sys.argv[1:]
    # imicon（输入法注册 iconName="voiceime" 对应文件；与搜狗同尺寸）
    write_png(draw_mic(48), os.path.join(ICON_DIR, "voiceime.png"))
    if install:
        # 1) fcitx imicon
        dst = "/usr/share/fcitx/imicon/voiceime.png"
        subprocess.check_call(["sudo", "-n", "install", "-m", "644",
                               os.path.join(ICON_DIR, "voiceime.png"), dst])
        print("已安装 imicon: %s" % dst)
        # 2) fcitx-voiceime.svg → Nde 主题 panel/ + actions/（面板托盘只用 SVG）
        svg = os.path.join(ICON_DIR, "fcitx-voiceime.svg")
        for theme in ("Dark", "Light", "Fashion"):
            root = "/usr/share/icons/%s" % theme
            for s in THEME_SIZES:
                for sub in ("panel", "actions"):
                    subprocess.check_call(["sudo", "-n", "install", "-D", "-m", "644",
                                           svg, "%s/%dx%d/%s/fcitx-voiceime.svg" % (root, s, s, sub)])
            print("已安装 fcitx-voiceime.svg: %s" % theme)


if __name__ == "__main__":
    main()
