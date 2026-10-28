#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
豆包流式语音识别（火山引擎 SAUC 双向流式）客户端
================================================
与 asr_client.StreamingASR 同接口（start/send_audio/stop/close），供 voice_ime_service 切换后端。

- 鉴权：X-Api-Key / X-Api-Resource-Id / X-Api-Request-Id（WS 握手头，无签名）
- 端点：wss://openspeech.bytedance.com/api/v3/sauc/bigmodel（边说边出字）
- 音频：PCM 16kHz/16bit/单声道；二进制帧 = 4B 头 + seq(4B) + gzip 体
  首包 CLIENT_FULL_REQUEST(JSON 配置)，后续 CLIENT_AUDIO_ONLY_REQUEST(音频)；
  末包 seq 取负表示流结束
- 结果映射（enable_nonstream=true 二遍识别）：
  · definite=true 的 utterance → on_final（定稿句，VAD 静音 800ms 后触发）
  · 其余实时文本 → on_interim（preedit 预览）
"""
import gzip
import json
import logging
import ssl
import struct
import threading
import uuid

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

log = logging.getLogger("asr-doubao")

# 双向流式端点（interim 实时 + enable_nonstream 定稿）
URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
# 整句返回端点（备用）
URL_NOSTREAM = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"

_HEADER = b"\x11"          # version=1, header_size=1(块)


def _frame(message_type, flags, seq, body: bytes) -> bytes:
    """组二进制帧：4B 头 + seq(>i) + 长度(>I) + gzip 体"""
    packed = gzip.compress(body)
    return (_HEADER + bytes([(message_type << 4) | flags, 0x11, 0x00]) +
            struct.pack(">i", seq) + struct.pack(">I", len(packed)) + packed)


def _json_frame(message_type, flags, seq, obj) -> bytes:
    return _frame(message_type, flags, seq, json.dumps(obj, ensure_ascii=False).encode())


class DoubaoASR:
    """单次录音会话的豆包流式转写客户端（同步模型，与 StreamingASR 一致）。"""

    def __init__(self, api_key, resource_id="volc.seedasr.sauc.duration",
                 url=URL, sample_rate=16000, sample_width=2, request=None):
        self.api_key = api_key
        self.resource_id = resource_id
        self.url = url
        self.sample_rate = sample_rate
        self.sample_width = sample_width
        self.request_overrides = request or {}   # config.doubao.request 覆盖默认参数
        self.ws = None
        self.finals = []                 # 定稿句文本（按序）
        self._nostream = "nostream" in url   # 单向流式：整句结果随 result.text 一次返回
        self._nostream_text = ""             # nostream 已识别文本（避免反复提交同一句）
        self._seq = 1
        self._on_interim = None
        self._on_final = None
        self._recv_thread = None
        self._completed = threading.Event()
        self._error = None
        self._done_ranges = set()        # 已提交的 definite 句 (start,end,text)
        self._last_interim = ""          # 上次 interim 文本（去重）

    # ── 生命周期 ──
    def start(self, on_interim=None, on_final=None):
        """建立 WS 连接并发送全量配置。阻塞直到连接就绪。"""
        headers = {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
        }
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # 内网 HTTPS 拦截（与 asr_client 同）
        self.ws = connect(self.url, additional_headers=headers,
                          ssl=ctx, open_timeout=10)
        payload = {
            "user": {"uid": "voiceime"},
            "audio": {
                "format": "pcm", "codec": "raw",
                "rate": self.sample_rate, "bits": self.sample_width * 8,
                "channel": 1,
            },
            "request": {
                "model_name": "bigmodel",
                "enable_punc": True, "enable_itn": True,
                "enable_ddc": False,
                "enable_accelerate_text": False,
                "enable_nonstream": True,
                "show_utterances": True,
                "result_type": "full",
                "end_window_size": 800,
                "force_to_speech_time": 1000,
                **self.request_overrides,   # config.doubao.request 覆盖（Web 界面可调）
            },
        }
        self._seq = 1
        self.ws.send(_json_frame(0b0001, 0b0001, self._seq, payload))  # CLIENT_FULL_REQUEST, POS_SEQUENCE
        self._seq = 2
        self._on_interim = on_interim
        self._on_final = on_final
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()
        return self

    def _recv_loop(self):
        try:
            while True:
                msg = self.ws.recv()
                if not isinstance(msg, bytes):
                    continue
                mtype = msg[1] >> 4
                flags = msg[1] & 0x0f
                p = msg[(msg[0] & 0x0f) * 4:]
                if flags & 0b0001:          # payload_sequence
                    p = p[4:]
                if flags & 0b0100:          # event
                    p = p[4:]
                if mtype == 0b1111:         # SERVER_ERROR_RESPONSE
                    code = struct.unpack(">i", p[:4])[0]
                    size = struct.unpack(">I", p[4:8])[0]
                    self._error = f"服务端错误码 {code}"
                    log.error("%s", self._error)
                    self._completed.set()
                    break
                if mtype != 0b1001:         # 忽略非 SERVER_FULL_RESPONSE
                    log.warning("未知响应帧 mtype=%d flags=%#x", mtype, flags)
                    continue
                size = struct.unpack(">I", p[:4])[0]
                body = p[4:4 + size]
                try:
                    if msg[2] & 0x0f == 0b0001:    # GZIP
                        body = gzip.decompress(body)
                    obj = json.loads(body.decode("utf-8", "replace"))
                except Exception:
                    log.warning("响应解析失败: flags=%#x ser=%#x comp=%#x body=%.120r",
                                flags, msg[2] >> 4, msg[2] & 0x0f, body)
                    continue
                code = obj.get("code", 0) if isinstance(obj, dict) else 0
                if code:
                    self._error = f"识别错误: {obj.get('message', obj)}"
                    log.error("%s", self._error)
                    self._completed.set()
                    break
                self._handle(obj, is_last=bool(flags & 0b0010 or obj.get("is_last_package")))
                if flags & 0b0010 or obj.get("is_last_package"):   # is_last_package
                    self._completed.set()
                    break
        except Exception as e:
            # 服务端正常结束流（收到负 seq 末包后 close(1000)）不算错误
            if not isinstance(e, ConnectionClosed):
                if not self._completed.is_set():
                    self._error = f"接收线程异常: {e}"
                    log.error("%s", self._error)
            self._completed.set()

    def _handle(self, obj, is_last=False):
        pm = obj.get("payload_msg")
        if not isinstance(pm, dict):
            pm = obj
        if not getattr(self, "_struct_logged", False):
            self._struct_logged = True
            log.info("响应结构: 根=%s pm=%s", sorted(obj.keys()), sorted(pm.keys()))
        r = pm.get("result")
        if not isinstance(r, dict):
            return
        if not getattr(self, "_frame_logged", False):
            self._frame_logged = True
            utts = r.get("utterances") or []
            log.info("result 结构: keys=%s utts=%d 首条=%s",
                     sorted(r.keys()), len(utts),
                     json.dumps(utts[0], ensure_ascii=False)[:400] if utts else "-")
        utts = r.get("utterances") or []
        # final：新增 definite 定稿句（按时间窗去重，full 模式每帧全量重发）
        for u in utts:
            if u.get("definite") and u.get("text", "").strip():
                t = u["text"].strip()
                key = (u.get("start_time"), u.get("end_time"), t)
                if key not in self._done_ranges:
                    self._done_ranges.add(key)
                    self.finals.append(t)
                    if self._on_final:
                        self._on_final({"text": t})
        # nostream：无 utterance，整句结果随 result.text 一次返回。末包到达即视为
        # 定稿提交（此前各帧 result.text 为累计中间值，只作 preedit 预览，不提交），
        # 避免"识别了却不上屏"（老 bug：识别文本只走 interim，从不 commit）
        if self._nostream and not utts:
            text = (r.get("text") or "").strip()
            # 末包 → 提交定稿；否则仅预览（same 文本去重，防每帧重复 on_interim）
            if is_last and text and text != self._nostream_text:
                self._nostream_text = text
                self.finals.append(text)
                if self._on_final:
                    self._on_final({"text": text})
            elif not is_last and text and text != self._last_interim:
                self._last_interim = text
                if self._on_interim:
                    self._on_interim({"text": text})
            return
        # interim：最后一句未定稿内容（已定稿句上屏后 preedit 只显示"正在说"的部分）
        live = [u["text"].strip() for u in utts
                if not u.get("definite") and u.get("text", "").strip()]
        if not live:
            text = r.get("text") or ""
            if text.strip() and not utts:
                live = [text.strip()]
        if live:
            text = live[-1]
            if text != self._last_interim:
                self._last_interim = text
                if self._on_interim:
                    self._on_interim({"text": text})

    # ── 发送 ──
    def send_audio(self, chunk: bytes):
        """发送一个音频分片（Recorder 200ms 节奏）。返回 False 表示连接不可用。"""
        if self.ws is None:
            return False
        try:
            self.ws.send(_frame(0b0010, 0b0001, self._seq, chunk))  # CLIENT_AUDIO_ONLY_REQUEST, POS_SEQUENCE
            self._seq += 1
            return True
        except Exception as e:
            self._error = f"音频发送失败: {e}"
            return False

    def stop(self, timeout=15):
        """发送流结束标记（负 seq 空包）并等待最终结果。返回定稿句列表。"""
        if self.ws is not None:
            try:
                self.ws.send(_frame(0b0010, 0b0011, -self._seq, b""))  # NEG_WITH_SEQUENCE（流结束）
            except Exception:
                pass
        self._completed.wait(timeout=timeout)
        self.close()
        return self.finals

    def close(self):
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass
        self.ws = None
