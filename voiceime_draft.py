#!/usr/bin/python3.6
"""voice-ime 草稿窗口：语音识别文本暂存 + 一键大模型润色。

录音中单独按 Ctrl（引擎→服务端转发）切换输出到草稿窗口并显示本窗口：
  - 句子完成（final）追加为新行，当前句（interim）实时更新为最后一行
  - 优化按钮：把草稿全文发给 config.json 的 llm 段配置的模型（OpenAI 兼容
    chat/completions），去除口头语/感叹词后显示在下半区，可复制

监听 UDP 127.0.0.1:19528（voice_ime_service.py 发送）：
  {"type":"text","mode":"interim","text":"..."}  更新当前句（替换最后一行）
  {"type":"text","mode":"final","text":"..."}    完成一句（追加为新行）
  {"type":"show"}    显示并置顶（录音中单按 Ctrl 触发）
  {"type":"clear"}   清空草稿
  {"type":"quit"}    退出进程
常驻；关闭/隐藏窗口 = 通知服务端退出草稿模式（恢复直接上屏）+ 清空内容，
下次打开是全新的。

config.json 的 llm 段（api_key 填 sk- 开头的密钥，或填环境变量名从环境读取）：
  system_prompt = 角色 + 全部润色规则（与自动润色共用同一份，内容见 config.example.json）
  prompt       = user 消息前缀，一般留空（提示词本身已按「上文/待润色」分段）
  auto_polish  = true 时核心服务在每句上屏前自动润色（本窗口不受影响）
  多模型写法见 README 的 llm.models[]，本窗口与核心服务取同一个 active 模型
"""
import json
import os
import socket
import threading
import time

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib   # noqa: E402

import llm_polish   # noqa: E402  润色请求实现（与核心服务共用，含 SSL/Key 解析）

UDP_ADDR = ("127.0.0.1", 19528)
WIN_W, WIN_H = 560, 420
POLL_MS = 40


def load_llm_cfg():
    """读 config.json 的 llm 段，展开当前生效模型（与核心服务一致）；异常返回空 dict"""
    return llm_polish.load_cfg()


class Draft:
    def __init__(self):
        self.lines = []        # 已完成的句子（一行一句）
        self.current = ""      # 正在识别的当前句
        self.optimized = False # 优化是否成功
        self.tail_mark = None      # 当前句预览尾巴起点 mark（interim 占位，可删）
        self._pending_final = ""   # 待追加的定稿句（UDP 循环内累积，GTK 主循环统一刷新）
        self._pending_current = "" # 待更新的当前句预览
        self._dirty = False
        self._shown = ""
        self.win = Gtk.Window(title="voice-ime 草稿")
        self.win.set_default_size(WIN_W, WIN_H)
        self.win.set_resizable(False)      # 固定尺寸，不可缩放
        self.win.set_keep_above(True)
        self.win.connect("delete-event", self.on_delete)

        # 原文区（可编辑：语音识别可能把专有名词转写错，用户可在优化前手动修正）
        self.tv = Gtk.TextView()
        self.tv.set_editable(True)
        self.tv.set_cursor_visible(False)
        self.tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.buf = self.tv.get_buffer()
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.tv)

        # 优化结果区（带标签）
        lbl_src = Gtk.Label(label="原文（草稿，可修改）", xalign=0)
        lbl_res = Gtk.Label(label="优化结果", xalign=0)
        self.tv_res = Gtk.TextView()
        self.tv_res.set_editable(False)
        self.tv_res.set_cursor_visible(False)
        self.tv_res.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.buf_res = self.tv_res.get_buffer()
        sw_res = Gtk.ScrolledWindow()
        sw_res.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw_res.add(self.tv_res)

        # 按钮栏：居中【复制全部】【优化】【复制优化结果】（状态提示在按钮上方）
        btn_copy = Gtk.Button(label="复制全部")
        btn_copy.connect("clicked", self.on_copy_all)
        btn_opt = Gtk.Button(label="优化")
        btn_opt.connect("clicked", self.on_optimize)
        btn_copy_res = Gtk.Button(label="复制优化结果")
        btn_copy_res.connect("clicked", self.on_copy_result)
        hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hb.pack_start(Gtk.Box(), True, True, 0)   # 左右占位 → 按钮居中
        hb.pack_start(btn_copy, False, False, 0)
        hb.pack_start(btn_opt, False, False, 0)
        hb.pack_start(btn_copy_res, False, False, 0)
        hb.pack_start(Gtk.Box(), True, True, 0)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vbox.pack_start(lbl_src, False, False, 0)
        vbox.pack_start(sw, True, True, 0)
        vbox.pack_start(lbl_res, False, False, 0)
        vbox.pack_start(sw_res, True, True, 0)
        self.lbl_status = Gtk.Label(label="", xalign=0.5)
        vbox.pack_start(self.lbl_status, False, False, 0)
        vbox.pack_start(hb, False, False, 0)
        self.win.add(vbox)

        # Esc 关窗退出草稿模式（焦点在窗口内时）
        self.win.connect("key-press-event", self.on_key_press)

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(UDP_ADDR)
        self.sock.setblocking(False)
        GLib.timeout_add(POLL_MS, self.tick)

    def on_delete(self, *args):
        """点窗口 X：退出草稿模式并清空，隐藏（保持常驻）"""
        self.close_draft()
        return True

    def close_draft(self):
        """关闭/隐藏草稿窗口：通知服务端退出草稿模式（恢复直接上屏）+ 清空内容，
        保证下次打开是全新的"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(json.dumps({"type": "draft_closed"}).encode(),
                     ("127.0.0.1", 19529))
            s.close()
        except Exception:
            pass
        self.lines = []
        self.current = ""
        self.optimized = False
        self._pending_final = ""
        self._pending_current = ""
        self.tail_mark = None
        self.buf.set_text("")
        self.buf_res.set_text("")
        self.lbl_status.set_text("")
        self.win.hide()

    def tick(self):
        while True:
            try:
                data, _ = self.sock.recvfrom(65536)
            except (BlockingIOError, OSError):
                break
            try:
                msg = json.loads(data.decode("utf-8", "replace"))
                if not isinstance(msg, dict):   # 合法 JSON 但不是对象会让下面的 .get 抛异常
                    continue                    # 打死这个 GLib timeout，窗口从此不刷新
            except Exception:
                continue
            t = msg.get("type")
            if t == "text":
                mode = msg.get("mode", "interim")
                text = msg.get("text", "")
                if mode == "final":
                    if text:
                        self.lines.append(text)
                        self._pending_final += ((" " if self._pending_final or self.current else "") + text)
                    self.current = ""
                    self._pending_current = ""
                else:
                    self.current = text
                    self._pending_current = text
            elif t == "show":
                self.win.show_all()
                self.win.present()          # 置顶显示（不抢焦点）
            elif t == "clear":
                self.lines = []
                self.current = ""
                self._pending_final = ""
                self._pending_current = ""
                self._drop_tail()
                self.buf.set_text("")
                self.buf_res.set_text("")
            elif t == "quit":
                Gtk.main_quit()
                return False
        if self._pending_final or self._pending_current or self._dirty:
            self._dirty = False
            self.refresh()
        return True

    def _drop_tail(self):
        """删除文末的当前句预览尾巴（interim 占位），保留已完成句与用户编辑"""
        if self.tail_mark is None:
            return
        try:
            start = self.buf.get_iter_at_mark(self.tail_mark)
            end = self.buf.get_end_iter()
            if start.compare(end) < 0:
                self.buf.delete(start, end)
        except Exception:
            pass
        self.tail_mark = None

    def _append_permanent(self, text):
        """把一条定稿句追加到文末（成为永久内容）"""
        if not text:
            return
        end = self.buf.get_end_iter()
        prefix = ""
        if not end.starts_line():
            # 前面已有内容：取最后一个字符判断是否需补空格
            prev = end.copy()
            prev.backward_char()
            ch = prev.get_char()
            if ch not in (" ", "\n"):
                prefix = " "
        self.buf.insert(end, prefix + text)

    def _append_tail(self, text):
        """把当前句预览追加到文末并记尾巴 mark（下次 interim 先删再插）"""
        self._drop_tail()
        if not text:
            return
        end = self.buf.get_end_iter()
        prefix = ""
        if not end.starts_line():
            prev = end.copy()
            prev.backward_char()
            if prev.get_char() not in (" ", "\n"):
                prefix = " "
        self.buf.insert(end, prefix + text)
        # mark 落在插入内容起点（left_gravity：删除时随左侧移动，稳定指向尾巴头）
        self.tail_mark = self.buf.create_mark(None, self.buf.get_iter_at_offset(
            self.buf.get_end_iter().get_offset() - len(prefix + text)), True)

    def draft_text(self):
        """当前草稿全文（buffer 内容，含已完成句 + 可能的当前句尾巴）"""
        return self.buf.get_text(self.buf.get_start_iter(),
                                 self.buf.get_end_iter(), True)

    def refresh(self):
        """按服务端消息刷新：定稿句永久追加、interim 做可替换尾巴。
        全程只动文末，绝不 set_text 全量重建——用户的光标位置和手动编辑
        在识别流继续时保持不动。"""
        # 必须先清掉上一条 interim 尾巴再追加：尾巴 mark 落在文末，反过来
        # 会把刚追加的定稿句一起删掉（表现为每句定稿丢失）
        if self.tail_mark is not None:
            self._drop_tail()
        if self._pending_final:
            self._append_permanent(self._pending_final)
            self._pending_final = ""
        if self._pending_current:
            self._append_tail(self._pending_current)
            self._pending_current = ""
        if self.buf_res.get_text(self.buf_res.get_start_iter(),
                                 self.buf_res.get_end_iter(), True).strip():
            self.buf_res.set_text("")   # 原文变化清掉旧优化结果（内容已变，结果过期）
        self._shown = self.draft_text()

    def on_copy_all(self, w):
        """复制原文区全部内容到剪贴板"""
        text = self.buf.get_text(self.buf.get_start_iter(),
                                 self.buf.get_end_iter(), True).strip()
        if not text:
            self.lbl_status.set_text("草稿为空，没有可复制的内容。")
            return
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        self.lbl_status.set_text("已复制 %d 字" % len(text))

    def on_copy_result(self, w):
        """复制优化结果区内容到剪贴板（须先点过「优化」）"""
        text = self.buf_res.get_text(self.buf_res.get_start_iter(),
                                     self.buf_res.get_end_iter(), True).strip()
        if not text or text.startswith("优化失败") or text == "优化中…":
            self.lbl_status.set_text("还没有可复制的优化结果，先点「优化」。")
            return
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        self.lbl_status.set_text("已复制优化结果 %d 字" % len(text))

    def on_key_press(self, w, ev):
        """窗口内按 Esc → 关闭草稿并退出草稿模式（等同点 X）"""
        if ev.keyval == 0xff1b:   # GDK_KEY_Escape
            self.close_draft()
            return True
        return False

    # ── 优化：调 config.json 配置的大模型 ────────────────────
    def on_optimize(self, w):
        cfg = load_llm_cfg()
        endpoint = cfg.get("endpoint", "").strip()
        if not endpoint:
            self.buf_res.set_text("未配置 LLM API：请先在 config.json 的 llm 段填写 endpoint/api_key/model，然后重启服务。")
            return
        # 取原文区当前内容：用户可先手动修正语音识别错误的专有名词再优化
        text = self.buf.get_text(self.buf.get_start_iter(),
                                 self.buf.get_end_iter(), True).strip()
        if not text:
            self.buf_res.set_text("草稿为空，先说话或清空后重试。")
            return
        if not llm_polish.resolve_key(cfg):
            self.buf_res.set_text("未找到 API Key：在 config.json 的 llm.api_key 直接填 sk- 开头的密钥，"
                                  "或填环境变量名并确保已 export（默认 DEEPSEEK_API_KEY）。")
            return
        model = cfg.get("model", "").strip()
        print("优化请求: endpoint=%s model=%s 文本长度=%d" % (endpoint, model, len(text)), flush=True)
        self.buf_res.set_text("优化中…")
        self.optimized = False
        threading.Thread(target=self._optimize_worker,
                         args=(cfg, text), daemon=True).start()

    def _optimize_worker(self, cfg, text):
        t0 = time.monotonic()
        try:
            result = llm_polish.polish(cfg, text, timeout=120)
            print("优化完成: model=%s 耗时=%.2fs" % (cfg.get("model"), time.monotonic() - t0), flush=True)
            print("  原文: %s" % text, flush=True)
            print("  结果: %s" % result, flush=True)
        except Exception as e:
            result = f"优化失败: {e}"
            print("优化异常: 耗时=%.2fs %s" % (time.monotonic() - t0, e), flush=True)
            print("  原文: %s" % text, flush=True)
        GLib.idle_add(self._set_result, result)

    def _set_result(self, result):
        self.buf_res.set_text(result)
        self.optimized = not result.startswith("优化失败")
        if self.optimized:
            # 优化成功：结果留在下方结果区，窗口保持打开——用户自行「复制全部」或 Esc 关闭
            # （不自动复制/自动关窗，避免打断连续整理多段语音的流程）
            self.lbl_status.set_text("优化完成。复制结果请点上方「复制优化结果」，或直接编辑原文。")
        else:
            self.lbl_status.set_text("")
        return False   # idle 回调只执行一次


def main():
    Draft()
    Gtk.main()


if __name__ == "__main__":
    main()
