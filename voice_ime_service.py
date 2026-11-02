#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
voice-ime 语音服务（fcitx 引擎后端）
=====================================
Unix socket IPC 服务：接收 fcitx 引擎命令，执行录音 + 流式 ASR，推送结果。

协议（JSON 行，\n 分隔）：
  引擎 → 服务: {"cmd":"start", "clipboard":true|false} / {"cmd":"stop"} /
               {"cmd":"cancel"} / {"cmd":"ping"}
  服务 → 引擎: {"type":"ready"}
               {"type":"preedit","text":"..."}   中间结果/状态提示
               {"type":"commit","text":"..."}    最终提交文本
               {"type":"done","ok":true|false}
"""
import array
import collections
import ctypes
import json
import logging
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from asr_doubao import DoubaoASR, URL_ASYNC
import llm_polish

SOCKET_PATH = "/tmp/voiceime-service.sock"
LOG_PATH = str(Path(__file__).resolve().parent / "logs" / "voiceime-service.log")
HISTORY_PATH = str(Path(__file__).resolve().parent / "logs" / "voiceime-history.jsonl")

log = logging.getLogger("voiceime-service")


def setup_logging():
    # 只写文件（不再 StreamHandler：service.sh 已把 stderr 重定向到同一日志，
    # 双 handler 导致每行重复打印）
    Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        # 统一格式: YYYY-MM-DD HH:MM:SS.mmm（点后 3 位为毫秒）
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.FileHandler(LOG_PATH)],
    )

_history_lock = threading.Lock()


def record_history(orig, text, polished=False, fallback=False, ms=0):
    """追加一条上屏记录（配置台「输入历史」页的数据源）。

    一行一条 JSON，O_APPEND 原子写；文件只追加，读取方取尾部 N 行。
    写失败不影响上屏。
    """
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "orig": orig, "text": text,
           "polished": bool(polished), "fallback": bool(fallback), "ms": int(ms)}
    try:
        with _history_lock, open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("写输入历史失败: %s", e)


BUF_MAX = 16   # 队列上限：16×200ms ≈ 3.2s。预热流空闲时也入队，必须有界，否则常驻占内存


# ── 录音：parec → 200ms 分片 → 路由到当前 ASR ──────────────
class Recorder(threading.Thread):
    def __init__(self, sample_rate, chunk_bytes, device="default",
                 silence_timeout_sec=0, on_silence=None, rms_threshold=300,
                 silence_after_input_sec=0, on_rms=None, on_send_fail=None):
        super().__init__(daemon=True, name="recorder")
        self.sample_rate = sample_rate
        self.chunk_bytes = chunk_bytes
        self.device = device
        self._proc = None
        self._stop_evt = threading.Event()
        self.ready_evt = threading.Event()   # 首块音频到达（parec 冷启动约 1.1s，见 _ensure_warm）
        # 静音超时自动停止（0 = 禁用）
        self.silence_timeout_sec = silence_timeout_sec
        self.on_silence = on_silence
        self.rms_threshold = rms_threshold
        self.silence_after_input_sec = silence_after_input_sec
        self.on_rms = on_rms          # 每 chunk 回调 RMS（浮窗声波动画用）
        self.on_send_fail = on_send_fail   # 发送线程发现连接已断时回调（触发续接）
        self._fail_fired = False
        self._silence_ms = 0
        self._heard_speech = False   # 是否已识别到有声（说过话后放宽静音阈值）
        # 音频路由：chunk → 当前 ASR 连接；切换窗口（无目标/发送失败）→ 缓存待补发
        self._route_lock = threading.Lock()
        self._target = None
        # 发送与录音解耦：parec 读取线程只入队，独立发送线程做 ws.send。
        # 网络阻塞只卡发送线程，录音照常读取——否则 parec 管道积压、音频丢失，
        # 且发包间隔超时会让服务端主动结束会话（bp 45000081 等包超时）。
        # 队列同时充当切换窗口的缓存：无目标时 sender 停在队首等待，音频不丢。
        self._buf = collections.deque(maxlen=BUF_MAX)
        self._buf_cv = threading.Condition()
        self._buf_gen = 0      # 清空代际：sender 取出但未发出的旧块据此丢弃

    def route_to(self, target):
        """切换音频发送目标（None = 缓存模式，等待新连接接管）"""
        with self._route_lock:
            self._target = target
        self._fail_fired = False      # 新目标接管：复位断线标记，允许再次触发续接
        with self._buf_cv:
            self._buf_cv.notify_all()

    def flush_pending(self):
        """新连接接管后等待积压音频发完（sender 自动等待 target 就绪）。
        没有发送目标时队列本来就发不出去，直接返回——否则会白等满超时
        （表现为"按下就松手"要 5 秒才收到 done）。"""
        with self._buf_cv:
            self._buf_cv.wait_for(
                lambda: not self._buf or self._target is None, timeout=5)

    def _dispatch(self, chunk):
        """只入队，永不阻塞录音线程"""
        with self._buf_cv:
            self._buf.append(chunk)
            self._buf_cv.notify()

    def _send_loop(self):
        """独立发送线程：按序消费队列，包不丢、不重排。"""
        while True:
            with self._buf_cv:
                while not self._buf:
                    if self._stop_evt.is_set():
                        return
                    self._buf_cv.wait(0.2)
                chunk = self._buf[0]
                gen = self._buf_gen
            # 等目标就绪（切换窗口期间音频留在队列里）。用条件变量而非 20ms 轮询：
            # 预热流空闲时长期停在这里，轮询等于常驻 50Hz 唤醒白耗 CPU。
            while True:
                with self._route_lock:
                    t = self._target
                if t is not None or self._stop_evt.is_set():
                    break
                with self._buf_cv:
                    self._buf_cv.wait(0.5)   # route_to() 会 notify_all
            if t is None:          # 已停止且无目标：丢弃剩余
                return
            with self._buf_cv:
                # 队列可能已被 clear_buffer 清空（按键前音频）：此时这块直接作废。
                # 不能无条件 popleft——空队列会抛 IndexError 打死发送线程，
                # 之后所有音频无人发送，服务端 10s 收不到包即报 45000081。
                stale = (gen != self._buf_gen or not self._buf
                         or self._buf[0] is not chunk)
                if not stale:
                    self._buf.popleft()
                # popleft 后必须 notify：flush_pending 的 wait_for 只在被唤醒或超时后
                # 才重查谓词，不唤醒就要干等满 timeout（表现为松手后浮窗多停 5 秒）
                self._buf_cv.notify_all()
            if stale:
                continue
            try:
                ok = t.send_audio(chunk)
            except Exception as e:
                log.warning("音频发送异常: %s", e)
                ok = False
            if not ok:
                log.warning("音频发送失败(连接已断),丢弃 1 包")
                if self.on_send_fail and not self._fail_fired:
                    self._fail_fired = True
                    try:
                        self.on_send_fail()
                    except Exception as e:
                        log.warning("断线续接回调异常: %s", e)

    def run(self):
        if self.device == "default":
            cmd = ["parec", "--rate", str(self.sample_rate),
                   "--channels", "1", "--format", "s16le"]
        else:
            cmd = ["parec", "-d", self.device, "--rate", str(self.sample_rate),
                   "--channels", "1", "--format", "s16le"]
        log.info("启动录音: %s", " ".join(cmd))
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._sender = threading.Thread(target=self._send_loop, daemon=True, name="asr-sender")
        self._sender.start()
        try:
            t0 = time.time()
            while not self._stop_evt.is_set():
                chunk = self._proc.stdout.read(self.chunk_bytes)
                if chunk and not self.ready_evt.is_set():
                    self.ready_evt.set()   # 浮窗据此从"未就绪"切到可说话
                    log.info("录音流就绪（首块音频 %.0f ms）", (time.time() - t0) * 1000)
                if not chunk:
                    # parec 意外退出（设备被拔、音频后端重启）：不回调的话会话会一直
                    # 卡在"录音中"，要等 300s 兜底轮换才复位
                    if not self._stop_evt.is_set():
                        log.warning("录音流意外结束（parec 退出）")
                        if self.on_died:
                            try:
                                self.on_died()
                            except Exception as e:
                                log.warning("录音流结束回调异常: %s", e)
                    break
                if len(chunk) & 1:                 # 收尾时可能读到奇数长度的 s16le 残片
                    chunk = chunk[:-1]             # 不裁会让下面的 frombytes 抛 ValueError
                self._dispatch(chunk)
                # RMS 计算：浮窗声波动画（on_rms）+ 静音超时自动停止
                samples = array.array("h")
                samples.frombytes(chunk)
                n = len(samples)
                rms = int((sum(s * s for s in samples) / n) ** 0.5) if n else 0
                if self.on_rms:
                    self.on_rms(rms)
                # 静音检测：RMS 低于阈值计为静音，累计超时触发自动停止。
                # 已识别到有声后（说过话）阈值放宽到 silence_after_input_sec，
                # 避免长话/念诗的自然停顿被误断。
                if self.silence_timeout_sec > 0:
                    chunk_ms = n * 1000 // self.sample_rate
                    if rms < self.rms_threshold:
                        self._silence_ms += chunk_ms
                        limit_ms = (self.silence_after_input_sec
                                    if self._heard_speech
                                    else self.silence_timeout_sec) * 1000
                        if self._silence_ms >= limit_ms:
                            log.info("连续静音 %d ms（RMS=%d，已说话=%s），自动停止录音",
                                     self._silence_ms, rms, self._heard_speech)
                            if self.on_silence:
                                self.on_silence()
                            break
                    else:
                        self._silence_ms = 0
                        self._heard_speech = True
        finally:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()

    def configure(self, silence_timeout_sec=0, on_silence=None, silence_after_input_sec=0,
                  on_rms=None, on_send_fail=None, rms_threshold=300, on_died=None):
        """按会话装配本轮参数。预热流常驻，参数每轮重新装配；空闲时传全默认 = 复位。"""
        self.silence_timeout_sec = silence_timeout_sec
        self.on_silence = on_silence
        self.silence_after_input_sec = silence_after_input_sec
        self.on_rms = on_rms
        self.on_send_fail = on_send_fail
        self.rms_threshold = rms_threshold
        self.on_died = on_died
        self._silence_ms = 0
        self._heard_speech = False

    def clear_buffer(self):
        """丢弃缓存音频：预热期间采到的（按键前）内容不上送，只上传按住之后的。"""
        with self._buf_cv:
            self._buf.clear()
            self._buf_gen += 1   # 让已取出、尚未发送的那一块也作废
            self._buf_cv.notify_all()

    def idle(self):
        """回到空闲预热态：停发、清缓存、复位会话参数；parec 继续跑，下次按下即用。"""
        self.route_to(None)
        self.clear_buffer()
        self.configure()

    def stop_and_drain(self):
        if self._proc is None:
            self._stop_evt.set()
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        # 先收完管道残余（此时发送线程仍在消费），再停
        try:
            while True:
                chunk = self._proc.stdout.read(self.chunk_bytes)
                if not chunk:
                    break
                self._dispatch(chunk)
        except Exception as e:
            log.warning("读取录音残余失败: %s", e)
        self._stop_evt.set()
        with self._buf_cv:
            self._buf_cv.notify_all()
        self.join(timeout=2)
        # 等发送线程把队列发完（切换续录时靠它保证尾部音频不丢）
        with self._buf_cv:
            self._buf_cv.wait_for(lambda: not self._buf, timeout=3)
        log.info("录音结束")


# ── 提示音（paplay 播预生成 wav，零依赖） ──────────────────
BEEP_START = "/tmp/voiceime-beep-start.wav"
BEEP_STOP = "/tmp/voiceime-beep-stop.wav"

def _gen_beep(path, freq, ms):
    import math
    import struct
    import wave
    rate = 22050
    try:
        with wave.open(path, "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            n = int(rate * ms / 1000)
            frames = b"".join(
                struct.pack("<h", int(0.3 * 32767 * math.sin(2 * math.pi * freq * t / rate)))
                for t in range(n)
            )
            w.writeframes(frames)
    except Exception:
        pass

def _play_beep(path):
    try:
        subprocess.Popen(["paplay", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def init_beeps():
    _gen_beep(BEEP_START, 880, 100)   # 短促高音：开始
    _gen_beep(BEEP_STOP, 440, 200)    # 低长音：结束

# ── X 焦点监控（ctypes 调 libX11，零依赖；由 focus_lost_stop 配置启用）──
class XFocus:
    """查询当前 X 焦点窗口。DISPLAY 不可用时禁用"""

    def __init__(self):
        self._dpy = None
        self._lock = threading.Lock()
        try:
            self._x11 = ctypes.CDLL("libX11.so.6")
            self._x11.XOpenDisplay.restype = ctypes.c_void_p
            self._x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            self._x11.XGetInputFocus.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int)]
            self._x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
            self._dpy = self._x11.XOpenDisplay(None)
        except Exception:
            self._dpy = None
        if not self._dpy:
            log.warning("X 焦点监控不可用（无法打开 DISPLAY），focus_lost_stop 不生效")

    def current(self):
        """当前焦点窗口 XID；不可用返回 None"""
        if not self._dpy:
            return None
        with self._lock:
            focus = ctypes.c_ulong(0)
            revert = ctypes.c_int(0)
            try:
                self._x11.XGetInputFocus(self._dpy, ctypes.byref(focus), ctypes.byref(revert))
                return int(focus.value)
            except Exception:
                return None


# ── 服务 ────────────────────────────────────────────────────
class VoiceIMEService:
    OSD_ADDR = ("127.0.0.1", 19527)   # 浮窗（voiceime_osd.py）UDP 监听
    DRAFT_ADDR = ("127.0.0.1", 19528) # 草稿窗口（voiceime_draft.py）UDP 监听
    DRAFT_CTRL_ADDR = ("127.0.0.1", 19529)  # 草稿窗口 → 服务端 控制消息（关闭退出草稿模式）

    def __init__(self, config_path):
        self.cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.config_path = config_path   # 豆包参数（热词等）每次录音前重读，改完不必重启
        self.clients = []          # 所有活动连接的 send 函数（调试/广播用）
        self.clients_lock = threading.Lock()
        self.active_sessions = []  # 所有活动 Session（信号退出时停录音、释放麦克风）
        self.observers = []        # 托盘等状态订阅者（tray 录音状态 / tray_rms 音量推送）
        self._rec_count = 0        # 全局录音中会话数（托盘状态依据）
        self._im_active = False    # 语音输入法是否为当前激活输入法（引擎 im_active 上报）
        self._osd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._draft_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._xfocus = XFocus()
        self._mode_b = False        # 录音模式：A 持续输入（默认）/ B 即停（Shift 切换）
        self._warm = None           # 常驻预热录音流（见 _take_recorder）
        self._bad_key = None         # 上次握手 401/403 的 key：再次按下时立刻提示，不必等一轮握手

    def _osd_send(self, obj):
        """向桌面浮窗发状态（录音中 RMS / stop 隐藏），失败忽略"""
        try:
            self._osd_sock.sendto(json.dumps(obj).encode(), self.OSD_ADDR)
        except Exception:
            pass

    def _current_api_key(self):
        """当前生效的 API Key（每次现读 config，与 _make_asr 同源）"""
        try:
            d = json.loads(Path(self.config_path).read_text(encoding="utf-8")).get("doubao") or {}
        except Exception:
            d = self.cfg.get("doubao") or {}
        return (d.get("api_key") or "").strip()

    def _draft_send(self, obj):
        """向草稿窗口发识别文本/显示命令，失败忽略（草稿窗口未启动等）"""
        try:
            self._draft_sock.sendto(json.dumps(obj, ensure_ascii=False).encode(), self.DRAFT_ADDR)
        except Exception:
            pass

    def _draft_ctrl_listener(self):
        """监听草稿窗口控制消息：窗口关闭/隐藏 → 退出草稿模式（恢复直接上屏）"""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(self.DRAFT_CTRL_ADDR)
        while True:
            try:
                data, _ = s.recvfrom(4096)
            except Exception:
                continue
            try:
                msg = json.loads(data.decode("utf-8", "replace"))
            except Exception:
                continue
            if msg.get("type") == "draft_closed":
                with self.clients_lock:
                    for sess in list(self.active_sessions):
                        if sess.drafting:
                            sess.drafting = False
                            log.info("草稿窗口关闭：会话恢复直接上屏")

    def broadcast(self, obj):
        with self.clients_lock:
            targets = list(self.clients)   # 快照后解锁：sendall 可能阻塞，不能占着全局锁
        for send in targets:
            try:
                send(obj)
            except Exception:
                pass

    # ── 托盘（voiceime_tray.py）状态推送 ──
    def _tray_state(self):
        """向托盘订阅者推送录音状态（全局是否在录音）"""
        with self.clients_lock:
            rec = self._rec_count > 0
            targets = list(self.observers)
        for s in targets:
            try:
                s.send({"type": "tray", "recording": rec})
            except Exception:
                pass

    def _tray_rms(self, r):
        """向托盘订阅者推送实时音量（驱动图标声波动画）"""
        with self.clients_lock:
            targets = list(self.observers)
        for s in targets:
            try:
                s.send({"type": "tray_rms", "r": r})
            except Exception:
                pass

    def _tray_im(self, active):
        """向托盘订阅者推送输入法激活状态（图标随输入法切换：麦克风/键盘）"""
        with self.clients_lock:
            for s in list(self.observers):
                try:
                    s.send({"type": "tray_im", "active": bool(active)})
                except Exception:
                    pass

    def _on_rms(self, r):
        self._osd_send({"rms": r})
        self._tray_rms(r)

    def stop_all(self):
        """全局停止：非录音连接（如托盘菜单）请求停止当前所有录音会话"""
        with self.clients_lock:
            # rec 或 asr 任一存在即视为活动会话（60s 切换空窗期 asr=None 但 rec 在录音）
            targets = [s for s in self.active_sessions if s.rec or s.asr]
        for s in targets:
            threading.Thread(target=self.stop_session, args=(s,), daemon=True).start()

    # ── 会话（每连接独立）──
    class Session:
        def __init__(self, send):
            self.send = send
            self.asr = None
            self.rec = None
            self.lock = threading.Lock()
            self.committed = 0      # 已即时 commit 的 final 条数（stop 时跳过）
            self.generation = 0     # 会话代际：start 递增，旧 auto_stop 定时器据此失效
            self.drafting = False   # 录音中单按 Ctrl 切换：识别结果进草稿窗口（默认直接上屏）
            self.clipboard_output = False
            self.clipboard_parts = []
            self.last_interim_text = ""  # 最新累积 interim 文本（停顿时兜底提交，防末句丢失）
            # 自动润色：串行队列保证上屏顺序（润色慢于后续句到达时，绝不能乱序）
            self.polish_q = queue.Queue()
            self.polish_worker = None   # 每会话单 worker 线程，懒启动
            self.polish_outstanding = 0 # 队列中未上屏的条数（积压降级判定）
            self.polish_idle = threading.Event()
            self.polish_idle.set()      # 队列清空时置位（stop 等它再发 done）
            self.recent = []            # 已上屏句子的原文（润色上下文，最多 CONTEXT_N 句）
            self.cancel_pending = False # 启动握手期收到 stop（对讲机松开）：握手完成后自停
            self.stopping = False       # stop 已开始：on_final 停止即时提交，由 stop 统一收尾
            self.switching = False      # 正在换代续录：stop 需等它结束再收尾，否则缓存音频无人消费

    def _make_asr(self, session):
        """构造 ASR 客户端（豆包 bigmodel_async），回调接口：interim / final"""
        def _cbs():
            return (lambda p: self.on_interim(session, p),
                    lambda p: self.on_final(session, p))
        d = self.cfg.get("doubao") or {}
        try:   # 热词 / API Key / 识别参数热更新：配置台保存后，下一句录音就用新值
            d = json.loads(Path(self.config_path).read_text(encoding="utf-8")).get("doubao") or d
        except Exception as e:
            log.warning("重读 config.json 的 doubao 段失败，沿用启动时配置: %s", e)
        if d.get("hotwords"):
            log.info("热词 %d 个: %s", len(d["hotwords"]), ", ".join(map(str, d["hotwords"][:10])))
        # 优化双向流式端点（bigmodel_async）：实测逐句 definite 正常，
        # 帧数最少且有实时 preedit 预览。单次会话不再需要按 15s 切段
        # （该限制来自 espeak 噪声音频的误测，长音频实测无截断）。
        return DoubaoASR(
            api_key=d.get("api_key", ""),
            resource_id=d.get("resource_id", "volc.seedasr.sauc.duration"),
            request=d.get("request"),
            hotwords=d.get("hotwords"),
            sample_rate=self.cfg.get("sample_rate", 16000),
            url=URL_ASYNC,
        ).start(*_cbs())

    def on_interim(self, session, p):
        text = (p.get("text", "") or "").strip()
        log.info("interim: %s", text)
        session.last_interim_text = text   # 停止时的兜底文本（末句未成 final 也不丢）
        if session.drafting:
            # 草稿模式（录音中单按 Ctrl 切换）：中间结果进草稿窗口，不上屏
            self._draft_send({"type": "text", "mode": "interim", "text": p["text"]})
        elif session.clipboard_output:
            pass
        else:
            session.send({"type": "preedit", "text": p["text"]})
            # 浮窗只显示声波：文本已上屏/在候选栏，不再重复显示（避免遮挡）

    # ── 自动润色（llm.auto_polish）──
    def _commit_text(self, session, text):
        """提交最终文本；开启自动润色时先送 DeepSeek 润色再上屏。

        顺序保证：文本一律经由会话的串行队列——即使某句跳过润色也排队，
        否则原文会插到正在润色的前一句之前（commit 按到达顺序追加）。
        积压（队列里已有未上屏句）时本句跳过润色，把延迟限制在单次润色内。
        配置每次现读，改开关立即生效（无需重启 core）。
        注意 done/preedit 仍由调用方同步发送：引擎收到 done 会复位 recording，
        延迟发送会让新一轮录音的 started 被旧 done 覆盖。
        """
        cfg = llm_polish.load_cfg()
        auto = bool(cfg.get("auto_polish")) and not session.drafting
        with session.lock:
            backlog = session.polish_outstanding > 0
            polish = auto and not backlog
            if auto and backlog:
                log.info("润色积压，本句直接提交原文: %r", text)
            session.polish_outstanding += 1
            session.polish_idle.clear()
            if session.polish_worker is None:
                session.polish_worker = threading.Thread(
                    target=self._polish_loop, args=(session,), daemon=True)
                session.polish_worker.start()
            session.polish_q.put((text, polish))

    @staticmethod
    def _split_sentences(text):
        """按中文/英文句末标点切语义句；无标点（说话未停顿整段定稿）整体算一句"""
        import re as _re
        parts = _re.split(r"(?<=[。！？!?；;])", text)
        sents = [p.strip() for p in parts if p.strip()]
        return sents or ([text.strip()] if text.strip() else [])

    def _send_commit(self, session, text, orig, polished=False, fallback=False, ms=0):
        """上屏一条文本，并把原文按语义句记入润色上下文。

        上下文按"句"裁剪（最多 CONTEXT_N 句），不是按 ASR 定稿条数：
        用户连续说话时 VAD 800ms 才切一条 final（一条可含几十秒内容），
        按条裁剪会让 recent 累积成"最近 1 分钟所有内容"。
        """
        if session.clipboard_output:
            with session.lock:
                session.clipboard_parts.append(text)
        else:
            session.send({"type": "commit", "text": text})
        session.recent.extend(self._split_sentences(orig))
        del session.recent[:-llm_polish.CONTEXT_N]
        record_history(orig, text, polished=polished, fallback=fallback, ms=ms)

    def _polish_loop(self, session):
        """会话串行润色线程：逐条取队列 → 润色（若标记）→ 上屏。
        失败/超时提交原文（不丢字）。随会话（引擎连接）存续，daemon。
        润色等待期间把原句放进候选栏（preedit），commit 后才清空——
        消除 0.3-2s 的候选栏空白（用户说完话立即能看到自己说的内容）。"""
        while True:
            text, polish = session.polish_q.get()
            out = text
            polished = fallback = False
            ms = 0
            if polish:
                if not session.clipboard_output:
                    session.send({"type": "preedit", "text": text})   # 先显示原文，别让候选栏空着
                self._osd_send({"type": "polish", "state": "on"})          # P5: 声波变橙（润色中）
                try:
                    cfg = llm_polish.load_cfg()
                    if cfg.get("auto_polish"):
                        # 上下文 = 最近 CONTEXT_N 个语义句（可能来自最近 1-2 条 final）
                        ctx = list(session.recent[-llm_polish.CONTEXT_N:])
                        t0 = time.time()
                        out = llm_polish.polish(cfg, text, ctx)
                        ms = int((time.time() - t0) * 1000)
                        polished = True
                        log.info("润色 %.2fs: %r -> %r", time.time() - t0, text, out)
                except Exception as e:
                    log.warning("润色失败，提交原文: %s", e)
                    self._osd_send({"type": "polish", "state": "fail"})    # P3: 声波变红（润色失败）
                    out = text
                    fallback = True
                else:
                    self._osd_send({"type": "polish", "state": "off"})     # 润色成功，恢复
            self._send_commit(session, out, orig=text, polished=polished,
                              fallback=fallback, ms=ms)
            if not session.clipboard_output:
                session.send({"type": "preedit", "text": ""})   # commit 已上屏，清空候选栏
            with session.lock:
                session.polish_outstanding -= 1
                if session.polish_outstanding <= 0:
                    session.polish_outstanding = 0
                    session.polish_idle.set()

    def on_final(self, session, p):
        """停顿分句：drafting 时追加进草稿；否则每句 final 立即提交，
        避免 preedit 被新句覆盖、内容丢失"""
        text = p.get("text", "").strip()
        if not text:
            return
        with session.lock:
            if session.stopping:
                # stop 已开始（用户已松手）：不再即时提交，该句由 stop_session 的
                # finals[committed:] 统一收尾。否则 on_final 与 stop 对同一 final
                # 双提交 → 输入框重复输出（一句话即松手时高频复现）
                return
            session.committed += 1
        # definite 已上屏该句：清兜底文本，防停止时重复提交
        session.last_interim_text = ""
        if session.drafting:
            self._draft_send({"type": "text", "mode": "final", "text": text})
        else:
            self._commit_text(session, text)   # 自动润色开启时先润色再上屏
            # preedit 清空由润色 worker 在 commit 后统一处理
            # （此前这里立即清空会让润色等待期候选栏空白 0.3-2s）

    def _take_recorder(self, chunk_bytes):
        """本轮录音流：优先复用常驻预热流；warm_mic=false 或采集参数变了才新建。"""
        want = (self.cfg["sample_rate"], chunk_bytes, self.cfg.get("audio_device", "default"))
        warm = bool(self.cfg.get("warm_mic", True))
        w = self._warm
        if warm and w is not None and w.is_alive() and (w.sample_rate, w.chunk_bytes, w.device) == want:
            return w
        if w is not None:
            try:
                w.stop_and_drain()     # 参数变更或关闭预热：收掉旧流
            except Exception:
                pass
            self._warm = None
        rec = Recorder(*want)
        rec.start()
        rec.route_to(None)
        if warm:
            self._warm = rec
        return rec

    def _beep_when_ready(self, rec):
        """开始提示音兼就绪信号：首块音频到手才响，用户听到之后再开口就不会被吞字。"""
        def fire():
            self._osd_send({"type": "ready"})
            _play_beep(BEEP_START)
        if rec.ready_evt.is_set():
            fire()
            return
        def wait():
            if rec.ready_evt.wait(timeout=3):
                fire()
        threading.Thread(target=wait, daemon=True).start()

    def preheat(self):
        """服务启动即拉起常驻录音流：第一次按下也不用等 parec 冷启动。"""
        if not self.cfg.get("warm_mic", True):
            return
        try:
            self._take_recorder(self.cfg["sample_rate"] * 2 * self.cfg.get("chunk_ms", 200) // 1000)
        except Exception as e:
            log.warning("预热录音流失败: %s", e)

    def start_session(self, session, silent=False, clipboard=False):
        # ── 锁快路径：只做本地装配（毫秒级），网络握手交给后台线程 ──
        with session.lock:
            if session.asr or session.rec:
                return   # 已在录音（浮窗持续指示中）
            if session.stopping:
                session.send({"type": "done", "ok": False})
                return
            session.cancel_pending = False   # 新 start 清除历史取消标记
            session.stopping = False
            session.committed = 0   # 本轮从零计数：不漏重置会跨轮错位 finals[committed:] 切片
            # 每次录音默认直接上屏：上一轮录音里切过的草稿模式不延续到本轮
            session.drafting = False
            session.clipboard_output = bool(clipboard)
            session.clipboard_parts = []
            # 新一轮录音 = 新的语义上下文（Session 随引擎连接常驻，不清会串上一轮内容）
            session.recent = []

            chunk_bytes = self.cfg["sample_rate"] * 2 * self.cfg.get("chunk_ms", 200) // 1000
            silence_sec = int(self.cfg.get("silence_timeout_sec", 15) or 0)
            rms_threshold = int(self.cfg.get("silence_rms_threshold", 300))
            after_input_sec = int(self.cfg.get("silence_after_input_sec", 30) or 0)
            session.generation += 1
            gen = session.generation

            def on_silence():
                # 静音超时 → 自动停止（与 Escape 同效果：停止并提交）。
                # 回调在录音读线程内，必须起独立线程调 stop_session（避免 join 死锁）。
                # 不携带代际：本录音流静音达标后随即 break 退出，其触发的停止必然
                # 针对当前会话（新 start 会先 stop_and_drain 杀掉本流，旧回调不会再
                # 触发，不会误停新会话）。若携带启动时代际，60s 切换换代后静音停止
                # 会被 stop_session 的代际检查拒绝 → 录音流退出而会话卡在"录音中"，
                # 浮窗消失、快捷键失灵（必须 ESC 才能恢复）。
                threading.Thread(target=self.stop_session,
                                 args=(session,), daemon=True).start()

            def on_send_fail():
                # 发送线程发现连接已断：立即换代续接（不等 300s 兜底轮换，
                # 否则这一整次说话全丢）。回调在 sender 线程内，起独立线程切换。
                threading.Thread(target=self.switch_session,
                                 args=(session, session.generation), daemon=True).start()

            def on_died():
                # parec 意外退出：按停止收尾，否则引擎永远停在"录音中"
                threading.Thread(target=self.stop_session,
                                 args=(session,), daemon=True).start()

            # 录音流：默认复用常驻预热流——parec 从启动到首块音频要 1.1 秒
            # （xrdp 音频源冷启动，各种采样率/缓冲参数都一样），这段时间麦克风
            # 根本没在采，用户看到浮窗就开口，开头一两个字必丢。
            rec = self._take_recorder(chunk_bytes)
            rec.configure(silence_timeout_sec=silence_sec,
                          on_silence=on_silence if silence_sec > 0 else None,
                          rms_threshold=rms_threshold,
                          silence_after_input_sec=after_input_sec,
                          on_rms=self._on_rms,
                          on_send_fail=on_send_fail,
                          on_died=on_died)
            rec.clear_buffer()     # 预热期（按键前）采到的音频不上送
            rec.route_to(None)     # 暂存音频，等 ASR 接管后 flush_pending 补发
            session.rec = rec
            # 浮窗：未就绪先画灰线，首块音频到达再转彩色声波
            self._osd_send({"type": "show", "ready": rec.ready_evt.is_set()})
            # 这个 key 上次被服务端 401 拒绝过：按下即提示，别让用户说完一整句才发现无效
            _key = self._current_api_key()
            if _key and _key == self._bad_key:
                self._osd_send({"type": "error",
                                "text": "语音密钥无效 · 请在配置台「校验 Key」后保存"})
        if not silent:
            self._beep_when_ready(rec)   # 提示音就是"可以开口了"的信号，不等 WebSocket 握手
        with self.clients_lock:
            self._rec_count += 1
        self._tray_state()
        # ASR 握手放后台：慢连接不再冻结控制通道（模式/草稿/cancel 照常响应）
        threading.Thread(target=self._attach_asr, args=(session, gen), daemon=True).start()
        # 录音指示由桌面浮窗（voiceime_osd.py 声波动画）呈现，不再用候选栏文字
        # 单次会话兜底上限：静默切换 ASR 会话续录，
        # 录音流不中断（parec 持续），切换窗口音频缓存补发 → 内容零丢失
        def auto_stop():
            time.sleep(self._rotate_sec())
            self.switch_session(session, gen)
        threading.Thread(target=auto_stop, daemon=True).start()

        # focus_lost_stop=true：失去焦点自动停止录音并释放麦克风
        if (not clipboard and self.cfg.get("focus_lost_stop", False)
                and self._xfocus.current() is not None):
            threading.Thread(target=self._watch_focus,
                             args=(session, gen), daemon=True).start()

    def _attach_asr(self, session, gen):
        """后台建立 ASR，完成后接管已在录音的音频流（缓存先行，内容零丢失）"""
        with session.lock:
            if session.generation != gen or session.rec is None:
                return      # 还没开始握手，会话就被停止/换代了
        try:
            asr = self._make_asr(session)
        except Exception as e:
            log.error("ASR 启动失败: %s", e)
            msg = str(e)
            bad_key = ("401" in msg or "403" in msg or "unauthor" in msg.lower())
            if bad_key:
                self._bad_key = self._current_api_key()
                hint = "语音密钥无效 · 请在配置台「校验 Key」后保存"
                log.error("浮窗提示: %s", hint)
                self._osd_send({"type": "error", "text": hint})
            if session.drafting:
                self._draft_send({"type": "text", "mode": "interim", "text": f"ASR 失败: {e}"})
            elif not bad_key:
                # 密钥无效已由浮窗提示：不再往输入框塞错误文本，免得被当成正文提交
                session.send({"type": "preedit", "text": f"ASR 失败: {e}"})
            # 录音已在跑：按停止收尾并复位引擎，避免浮窗卡在录音态
            threading.Thread(target=self.stop_session, args=(session, gen, False),
                             daemon=True).start()
            return
        self._bad_key = None      # 握手成功：清掉失败标记
        with session.lock:
            if session.generation != gen or session.rec is None:
                log.info("握手期会话已停止（松开过早），丢弃 ASR")
                try:
                    asr.close()
                except Exception:
                    pass
                return
            session.asr = asr
            rec = session.rec
            cancelled = session.cancel_pending
        if cancelled:
            log.info("握手完成时用户已松手，直接收尾")
            threading.Thread(target=self.stop_session, args=(session, gen, False),
                             daemon=True).start()
            return
        if getattr(asr, "server_error", None):
            # 握手阶段就被拒（如 503 SERVER_BUSY）：别把死连接挂上录音流再去撞发送失败
            srv_err = asr.server_error
            log.error("ASR 服务端拒绝 %s %s，终止会话", srv_err[0], srv_err[1])
            msg = "ASR 服务端拒绝: %s %s" % srv_err
            if session.drafting:
                self._draft_send({"type": "text", "mode": "interim", "text": msg})
            else:
                session.send({"type": "preedit", "text": msg})
            try:
                asr.close()
            except Exception:
                pass
            threading.Thread(target=self.stop_session, args=(session, gen, False),
                             daemon=True).start()
            return
        rec.route_to(asr)      # 接管：先补发缓存音频，再实时发送
        rec.flush_pending()

    def _watch_focus(self, session, gen):
        """录音期间轮询 X 焦点；焦点离开启动窗口 → 自动停止（丢弃剩余，避免提交到新窗口）"""
        focus_win = self._xfocus.current()
        if focus_win is None:
            return
        while True:
            time.sleep(0.3)
            with session.lock:
                if session.generation != gen or not session.asr:
                    return
            cur = self._xfocus.current()
            if cur is None:
                continue
            if cur != focus_win:
                log.info("focus_lost_stop：焦点切换 %#x -> %#x，自动停止录音（释放麦克风）",
                         focus_win, cur)
                threading.Thread(target=self.stop_session,
                                 args=(session, gen, False, True), daemon=True).start()
                return

    def _rotate_sec(self):
        """单次 ASR 会话的兜底上限（正常一次说话远到不了）。

        豆包 async 端点长音频实测无截断：30s 单会话完整识别、逐句 definite
        定稿。旧的"只认前 15 秒"结论来自 espeak 噪声音频的误测，已推翻。
        保留 300s 只为防连接老化，不再按 12s 切段（切段会让长句被拆成
        多段上屏 + 分段润色）。"""
        return 300

    def switch_session(self, session, gen):
        """会话上限到期：旧会话停止提交 → 新会话接管同一录音流（无缝续录）"""
        with session.lock:
            if session.generation != gen or session.asr is None:
                return
            # 立即换代并摘除 asr：切换的"空窗期"（old_asr.stop + new_asr.start
            # 阻塞，可达数秒）内到达的停止请求（静音超时/ESC/cancel）必须能终止
            # 会话。若等新 ASR 建好才换代，空窗期的 stop_session 会以旧代际通过
            # 检查杀掉录音流，而此处随后又无条件挂上新 ASR → 会话复活却无录音流，
            # 浮窗消失、快捷键失灵（必须 ESC 才能恢复）。
            session.generation += 1
            new_gen = session.generation
            old_asr = session.asr
            session.asr = None
            session.rec.route_to(None)   # 切换窗口：音频进缓存
            session.switching = True
        try:
            try:
                finals = old_asr.stop(timeout=5)      # 旧会话收尾（0.6s 内）
                if getattr(old_asr, "_error", None):
                    log.warning("ASR 错误（服务端返回）: %s", old_asr._error)
            except Exception as e:
                log.warning("旧会话收尾异常: %s", e)
                finals = []
            srv_err = getattr(old_asr, "server_error", None)
            if srv_err:
                # 服务端明确拒绝（503 SERVER_BUSY / 鉴权失败）：重连必然同样失败。
                # 继续切换会形成每 ~120ms 一轮的重试风暴（发送失败 → 切换 → 再失败），
                # 一次说话里建连几十次，用户侧只看到"录完不出字"。直接终止并说明原因。
                log.error("ASR 服务端拒绝 %s %s，终止会话（不重连）",
                          srv_err[0], srv_err[1])
                msg = "ASR 服务端拒绝: %s %s" % srv_err
                if session.drafting:
                    self._draft_send({"type": "text", "mode": "interim", "text": msg})
                else:
                    session.send({"type": "preedit", "text": msg})
                threading.Thread(target=self.stop_session,
                                 args=(session, new_gen, False), daemon=True).start()
                return
            # stop 返回前 on_final 已同步处理完 finals（completed 在其后置位），重读最新
            # committed；若在 stop 前读旧值，on_final 刚提交的句会被 rem 再次提交 → 重复
            with session.lock:
                old_committed = session.committed
            rem = "".join(finals[old_committed:]).strip()
            if not rem:
                # 兜底：服务端未返回 final（如切换瞬间仍在说话）时用累积 interim 文本
                with session.lock:
                    rem = (session.last_interim_text or "").strip()
            log.info("会话切换：旧会话提交 %r（finals %d 句）", rem, len(finals) - old_committed)
            if rem:
                if session.drafting:
                    self._draft_send({"type": "text", "mode": "final", "text": rem})
                else:
                    self._commit_text(session, rem)   # 自动润色开启时先润色再上屏
                    session.polish_idle.wait(timeout=llm_polish.DEFAULT_TIMEOUT + 3)
            # 这里不能发 done/started 给引擎：done 会复位 recording 和所有修饰键
            # 标志，用户仍按着触发键时引擎会以为已松手，之后真松手不再发 stop
            # （会话卡到静音超时），浮窗也会闪一下。续录期间录音流没断，引擎
            # 状态无需改动。
            try:
                new_asr = self._make_asr(session)
            except Exception as e:
                log.error("续录失败: %s", e)
                if session.drafting:
                    self._draft_send({"type": "text", "mode": "interim", "text": f"续录失败: {e}"})
                else:
                    session.send({"type": "preedit", "text": f"续录失败: {e}"})
                # 续录失败 = 会话死亡：收尾并复位引擎状态（换代已提前，必须用
                # new_gen 主动终止，旧代际的停止请求会被拒绝）
                threading.Thread(target=self.stop_session,
                                 args=(session, new_gen, False), daemon=True).start()
                return
            with session.lock:
                if session.rec is None or session.generation != new_gen:
                    # 空窗期会话已被停止（静音/ESC/cancel）或已重新 start：
                    # 丢弃新 ASR，保持当前状态，不复活会话
                    log.info("切换：会话已停止/换代，丢弃新 ASR")
                    try:
                        new_asr.close()
                    except Exception:
                        pass
                    return
                session.asr = new_asr
                session.committed = 0
                session.last_interim_text = ""   # 新会话从零累积，防重复提交
            session.rec.route_to(new_asr)     # 接管：先补发缓存音频再实时
            session.rec.flush_pending()
            def next_switch():
                time.sleep(self._rotate_sec())
                self.switch_session(session, new_gen)
            threading.Thread(target=next_switch, daemon=True).start()
        finally:
            with session.lock:
                session.switching = False

    def stop_session(self, session, gen=None, beep=True, drop=False):
        """停止录音并提交剩余文本；drop=True 时丢弃剩余（不提交，focus_lost_stop 自动停用）"""
        with session.lock:
            if gen is not None and session.generation != gen:
                return False   # 旧代际的定时器到期，会话已换代，忽略
            rec0 = session.rec
        # 会话切换/握手空窗期（asr 尚未挂上、rec 仍在录音）：此刻收尾会让这一轮
        # 缓存的音频没人消费而丢失（实测松手正好撞上 12s 切换 → 整段不上屏）。
        # 以"rec 是否还是同一条录音流"判断会话归属：切换会换代但 rec 不变，
        # 只有真正的新一轮 start/已收尾才换 rec。等新 ASR 挂上再正常收尾，
        # 最多 3s，避免引擎长时间卡在录音态。
        deadline = time.time() + 3
        while rec0 is not None and time.time() < deadline:
            with session.lock:
                if session.switching:
                    pass            # 换代续录进行中：等它挂上新 ASR 再收尾
                elif session.rec is not rec0 or session.asr is not None:
                    break
            time.sleep(0.05)
        with session.lock:
            if session.rec is not rec0:
                return False       # 已被另一路 stop 收尾，或新一轮录音已接管
            rec, asr = session.rec, session.asr
            session.rec = None
            session.asr = None
            session.stopping = True   # 挡 on_final 即时提交，剩余统一由本函数收尾
            clipboard_output = session.clipboard_output
        if rec is None and asr is None:
            # 会话本就空闲（观察者连接/重复 stop）：仅回 done。
            # 注意不能只看 asr：60s 切换的空窗期 asr=None 但 rec 仍在录音，
            # 此时必须继续走停止流程，否则录音流静默残留（浮窗消失、快捷键失灵）
            session.send({"type": "done", "ok": False})
            with session.lock:
                session.stopping = False
            return False
        with self.clients_lock:
            if self._rec_count > 0:
                self._rec_count -= 1
        self._tray_state()
        if rec:
            rec.flush_pending()    # 队列发完（原 stop_and_drain 的收尾语义）
            if rec is self._warm:
                rec.idle()         # 预热流常驻：回空闲态，不杀进程
            else:
                rec.stop_and_drain()
        self._osd_send({"type": "stop"})
        finals = asr.stop(timeout=5) if asr else []
        if asr is not None and getattr(asr, "_error", None):
            log.warning("ASR 错误（服务端返回）: %s", asr._error)
        # asr.stop() 阻塞期间 recv 线程仍可能处理迟到 final 并调 on_final；此时
        # stopping=True 已挡掉即时提交。但 committed 必须读 asr.stop 返回后的最新值：
        # 若在 stop 前读（旧值），on_final 已提交的句会被 remaining 再次覆盖 → 重复
        with session.lock:
            committed = session.committed
        if drop:
            # 焦点已切换：剩余内容丢弃（完整句已由 on_final 即时提交进原窗口），
            # 避免提交到切走后的新窗口
            log.info("focus_lost_stop：丢弃剩余 %d 条（已即时提交 %d 句）",
                     len(finals) - committed, committed)
            if session.drafting:
                # 草稿模式无窗口焦点问题：未 final 的剩余也进草稿，不丢内容
                rem_drop = "".join(finals[committed:]).strip()
                if rem_drop:
                    self._draft_send({"type": "text", "mode": "final", "text": rem_drop})
            session.send({"type": "preedit", "text": ""})
            session.send({"type": "done", "ok": False})
            with session.lock:
                session.stopping = False
            return True
        text = "".join(finals).strip()
        log.info("识别结果: %r (已即时提交 %d 句)", text, committed)
        remaining = finals[committed:]
        rem_text = "".join(remaining).strip()
        if not finals:
            # 服务端未返回 final（如刚起流即停止）：用累积 interim 兜底，避免丢字
            rem_text = (session.last_interim_text or "").strip()
            text = rem_text
        session.last_interim_text = ""
        if beep:
            _play_beep(BEEP_STOP)
        if rem_text:
            if session.drafting:
                # 草稿模式：剩余文本进草稿（不上屏）
                self._draft_send({"type": "text", "mode": "final", "text": rem_text})
            else:
                self._commit_text(session, rem_text)   # 自动润色开启时先润色再上屏
                # 等润色上屏后再发 done：done 会让引擎复位 recording，若先发，
                # 用户立刻开始下一轮录音时旧句会落到新窗口
                session.polish_idle.wait(timeout=llm_polish.DEFAULT_TIMEOUT + 3)
        if clipboard_output:
            session.polish_idle.wait(timeout=llm_polish.DEFAULT_TIMEOUT + 3)
            with session.lock:
                clipboard_text = "".join(session.clipboard_parts).strip()
            if clipboard_text:
                self._osd_send({"type": "clipboard", "text": clipboard_text,
                                "message": "已复制到剪贴板"})
                log.info("剪贴板结果: %r", clipboard_text)
            session.send({"type": "preedit", "text": ""})
            session.send({"type": "done", "ok": bool(clipboard_text)})
        elif rem_text:
            session.send({"type": "preedit", "text": ""})
            session.send({"type": "done", "ok": True})
        elif text:
            session.send({"type": "preedit", "text": ""})
            session.send({"type": "done", "ok": True})
        else:
            session.send({"type": "preedit", "text": ""})
            session.send({"type": "done", "ok": False})
        with session.lock:
            session.stopping = False
        return True

    def cancel_session(self, session):
        with session.lock:
            rec, asr = session.rec, session.asr
            session.rec = None
            session.asr = None
        if rec or asr:
            # 仅真正停止录音的会话才递减计数（观察者等无录音连接断开不误减）
            with self.clients_lock:
                if self._rec_count > 0:
                    self._rec_count -= 1
            self._tray_state()
        if rec:
            try:
                if rec is self._warm:
                    rec.idle()
                else:
                    rec.stop_and_drain()
            except Exception:
                pass
        self._osd_send({"type": "stop"})
        if asr:
            try:
                asr.close()
            except Exception:
                pass
        session.send({"type": "preedit", "text": ""})
        session.send({"type": "done", "ok": False})

    # ── 命令处理 ──
    def handle(self, line, session):
        try:
            msg = json.loads(line)
        except Exception:
            return
        cmd = msg.get("cmd")
        log.info("收到命令: %s", cmd)
        if cmd == "start":
            clipboard = bool(msg.get("clipboard"))
            threading.Thread(target=self.start_session,
                             args=(session, False, clipboard), daemon=True).start()
        elif cmd == "stop":
            # 对讲机松开/ESC：标记取消（覆盖启动握手期：start_session 完成握手后自停）
            with session.lock:
                session.cancel_pending = True
            if session.rec or session.asr:
                # 引擎等录音连接：会话级停止（rec/asr 任一存在即会话活动中，
                # 含 60s 切换空窗期 asr=None 但 rec 在录音的情况）
                threading.Thread(target=self.stop_session, args=(session,), daemon=True).start()
            else:
                with self.clients_lock:
                    is_observer = session in self.observers
                if is_observer:
                    # 托盘菜单"停止录音"：非录音连接，全局停止当前所有录音会话
                    self.stop_all()
                else:
                    # 引擎连接但 start 还没装配完：装配完成后会自行收尾，
                    # 这里只回 done 让引擎复位，绝不能连带停掉别人的会话
                    session.send({"type": "done"})
        elif cmd == "cancel":
            threading.Thread(target=self.cancel_session, args=(session,), daemon=True).start()
        elif cmd == "subscribe":
            # 托盘等状态观察者：订阅录音状态与实时音量推送
            with self.clients_lock:
                if session not in self.observers:
                    self.observers.append(session)
            session.send({"type": "tray", "recording": self._rec_count > 0})
            session.send({"type": "tray_im", "active": self._im_active})
        elif cmd == "im_active":
            # 引擎上报：语音输入法激活/失活（图标随输入法切换）
            active = bool(msg.get("active"))
            with self.clients_lock:
                self._im_active = active
            log.info("输入法激活状态: %s", active)
            self._tray_im(active)
        elif cmd == "mode":
            # 引擎上报/切换录音模式（A 持续输入 / B 即停）→ 浮窗显示
            b = bool(msg.get("b"))
            with self.clients_lock:
                self._mode_b = b
            log.info("录音模式: %s", "即停(B)" if b else "持续(A)")
            self._osd_send({"type": "mode", "b": 1 if b else 0})
        elif cmd == "broadcast":
            self.broadcast({"type": msg.get("type", "preedit"),
                            "text": msg.get("text", "")})
        elif cmd == "ping":
            session.send({"type": "ready"})
        elif cmd == "draft":
            # 引擎：切草稿模式（A 持续录音中单按 Ctrl / 空闲 Ctrl+Alt+Shift）
            session.drafting = not session.drafting
            log.info("draft 切换: drafting=%s", session.drafting)
            if session.drafting:
                self._draft_send({"type": "show"})
                # 把最近已上屏的历史句带入草稿（recent = 语义句原文，最多 CONTEXT_N 句），
                # 方便连录多段后统一编辑/优化
                with session.lock:
                    history = list(session.recent)
                for line in history:
                    self._draft_send({"type": "text", "mode": "final", "text": line})

    def run(self):
        init_beeps()
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        # 草稿窗口控制消息监听（关闭窗口 → 退出草稿模式）
        threading.Thread(target=self._draft_ctrl_listener, daemon=True).start()

        def _shutdown(signum, frame):
            log.info("收到信号 %s，停止所有录音并退出", signum)
            # 不能在 clients_lock 内调用 cancel_session：它内部同样获取该非可重入锁，
            # 会自死锁（进程收到 SIGTERM 后卡住不退出，systemd 停在 stop-sigterm，
            # 输入法随之起不来）。先取快照、释放锁，再逐个取消。
            with self.clients_lock:
                sessions = list(self.active_sessions)
            for s in sessions:
                try:
                    self.cancel_session(s)
                except Exception:
                    pass
            if self._warm is not None:
                try:
                    self._warm.stop_and_drain()
                except Exception:
                    pass
            os._exit(0)

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(SOCKET_PATH)
        server.listen(4)
        log.info("voice-ime 服务已启动: %s", SOCKET_PATH)
        while True:
            conn, _ = server.accept()
            log.info("引擎已连接")
            threading.Thread(target=self.client_loop, args=(conn,), daemon=True).start()

    def client_loop(self, conn):
        def send(obj):
            try:
                conn.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            except Exception as e:
                log.warning("发送失败: %s", e)
        session = self.Session(send)
        # 引擎连接建立：下发配置（长按阈值 + 两套触发快捷键）
        send({"type": "config",
              "hold_ms": int(self.cfg.get("hold_ms", 500)),
              "ptt_keys": self.cfg.get("ptt_keys", "ctrl+alt"),
              "hold_keys": self.cfg.get("hold_keys", "ctrl+alt+space")})
        with self.clients_lock:
            self.clients.append(send)
            self.active_sessions.append(session)
        buf = b""
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self.handle(line.decode("utf-8", "replace"), session)
        except Exception as e:
            log.warning("客户端断开: %s", e)
        finally:
            with self.clients_lock:
                if send in self.clients:
                    self.clients.remove(send)
                if session in self.active_sessions:
                    self.active_sessions.remove(session)
                if session in self.observers:
                    self.observers.remove(session)
            self.cancel_session(session)
            try:
                conn.close()
            except Exception:
                pass
            log.info("引擎断开")


def main():
    setup_logging()
    config_path = sys.argv[1] if len(sys.argv) > 1 else \
        str(Path(__file__).resolve().parent / "config.json")
    svc = VoiceIMEService(config_path)
    svc.preheat()
    svc.run()


if __name__ == "__main__":
    main()
