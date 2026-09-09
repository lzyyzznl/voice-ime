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

config.json 的 llm 段（api_key 字段是环境变量名，密钥从该环境变量读取）：
  system_prompt = 角色 + 全部润色规则 + 标签输出约束（每次请求不变）
  prompt       = user 消息的前缀，默认 "原文："（后面接待润色文本）
  "llm": {
    "endpoint": "https://api.deepseek.com/v1/chat/completions",
    "api_key": "DEEPSEEK_API_KEY",
    "model": "deepseek-v4-flash",
    "system_prompt": "你是文本润色助手。删除文本中的口头语、感叹词和重复（如 嗯、啊、那个、就是），修正不通顺的用词和语病，保持原意，使表达通顺自然。将润色后的文本放在 <output> 与 </output> 标签内输出，标签外不要输出任何内容。",
    "prompt": "原文："
  }
"""
import json
import os
import re
import socket
import threading
import time
import urllib.request

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib   # noqa: E402

UDP_ADDR = ("127.0.0.1", 19528)
WIN_W, WIN_H = 560, 420
POLL_MS = 40


def load_llm_cfg():
    """读 config.json 的 llm 段；缺失/异常返回空 dict"""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        with open(path, encoding="utf-8") as f:
            return (json.load(f).get("llm") or {}) or {}
    except Exception:
        return {}


class Draft:
    def __init__(self):
        self.lines = []        # 已完成的句子（一行一句）
        self.current = ""      # 正在识别的当前句
        self.optimized = False # 优化是否成功（复制优先复制优化结果）
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

        # 按钮栏：仅一个居中的优化按钮（状态提示在按钮上方）
        btn_opt = Gtk.Button(label="优化")
        btn_opt.connect("clicked", self.on_optimize)
        hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        hb.pack_start(Gtk.Box(), True, True, 0)   # 左右占位 → 按钮居中
        hb.pack_start(btn_opt, False, False, 0)
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
            except Exception:
                continue
            t = msg.get("type")
            if t == "text":
                mode = msg.get("mode", "interim")
                text = msg.get("text", "")
                if mode == "final":
                    if text:
                        self.lines.append(text)
                    self.current = ""
                else:
                    self.current = text
                self.refresh()
            elif t == "show":
                self.win.show_all()
                self.win.present()          # 置顶显示（不抢焦点）
            elif t == "clear":
                self.lines = []
                self.current = ""
                self.refresh()
            elif t == "quit":
                Gtk.main_quit()
                return False
        return True

    def draft_text(self):
        """当前草稿全文（完成句 + 未完成当前句）；停顿分句不换行，空格连接成连续文本"""
        parts = list(self.lines)
        if self.current:
            parts.append(self.current)
        return " ".join(parts)

    def refresh(self):
        self.buf.set_text(self.draft_text())

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
        prompt = cfg.get("prompt", "")
        self.optimized = False
        # api_key 字段存的是环境变量名（如 DEEPSEEK_API_KEY），取值时从环境读；
        # 留空则默认用 DEEPSEEK_API_KEY
        key_var = cfg.get("api_key", "").strip() or "DEEPSEEK_API_KEY"
        api_key = os.environ.get(key_var, "").strip()
        if not api_key:
            self.buf_res.set_text("未找到 API Key：环境变量 %s 未设置，请在启动环境中 export 后重启服务。" % key_var)
            return
        model = cfg.get("model", "").strip()
        system_prompt = cfg.get("system_prompt", "").strip()
        print("优化请求: endpoint=%s model=%s 文本长度=%d" % (endpoint, model, len(text)), flush=True)
        self.buf_res.set_text("优化中…")
        threading.Thread(target=self._optimize_worker,
                         args=(endpoint, api_key, model, system_prompt, prompt, text),
                         daemon=True).start()

    def _optimize_worker(self, endpoint, api_key, model, system_prompt, prompt, text):
        t0 = time.monotonic()
        try:
            messages = [{"role": "user",
                         "content": (prompt + "\n" + text) if prompt else text}]
            if system_prompt:
                messages.insert(0, {"role": "system", "content": system_prompt})
            body = {
                "model": model,
                "messages": messages,
                # v4 系列默认思考模式（high），润色任务不需要 → 显式关闭
                "thinking": {"type": "disabled"},
                "temperature": 0,   # 最低随机性：输出可预测、稳定
            }
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            if api_key:
                req.add_header("Authorization", "Bearer " + api_key)
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            raw = data["choices"][0]["message"]["content"].strip()
            # 提取 <output> 标签内内容；模型未按标签输出时回退用整体（容忍偏离）
            m = re.search(r"<output>(.*?)</output>", raw, re.S)
            result = m.group(1).strip() if m else raw
            print("优化完成: model=%s 耗时=%.2fs" % (model, time.monotonic() - t0), flush=True)
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
            # 优化成功：自动复制到剪贴板 + 提示 + 延迟自动关闭
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(result, -1)
            self.lbl_status.set_text("已复制到剪贴板，即将自动关闭…")
            GLib.timeout_add(1800, self._auto_close)
        else:
            self.lbl_status.set_text("")
        return False   # idle 回调只执行一次

    def _auto_close(self):
        self.close_draft()
        return False   # 只执行一次


def main():
    Draft()
    Gtk.main()


if __name__ == "__main__":
    main()
