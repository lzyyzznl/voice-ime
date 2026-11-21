#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DeepSeek 非流式语音识别客户端
================================
POST https://api.deepseek.com/v1/tools/asr
multipart/form-data 的 file 字段：16bit PCM WAV（或 Ogg Opus），单/双声道，
≤8 MiB、≤120s；一次请求整段音频，返回整段文本（不支持流式）。

接口与 DoubaoASR 对齐：start / send_audio / stop / close。
send_audio 只把 PCM 追加进内存，stop() 时拼 WAV 一次上传 —— 因此没有
interim 预览，识别结果在松手后 1~3s 一次返回。
"""
import http.client
import json
import logging
import ssl
import struct
import threading
import uuid
from urllib.parse import urlsplit

log = logging.getLogger("asr-deepseek")

URL = "https://api.deepseek.com/v1/tools/asr"
MAX_SEC = 120                 # 单请求时长上限（文档）
MAX_BYTES = 8 << 20           # 单请求文件上限（文档）
HTTP_TIMEOUT = 40             # 上传 3.8MB + 识别 120s 音频的余量（service 传的 5s 太短）

_ctx = None
_ctx_lock = threading.Lock()


def _ssl_ctx():
    """CERT_NONE：内网 HTTPS 拦截（自签证书），与 llm_polish 一致"""
    global _ctx
    with _ctx_lock:
        if _ctx is None:
            c = ssl.create_default_context()
            c.check_hostname = False
            c.verify_mode = ssl.CERT_NONE
            _ctx = c
        return _ctx


def _wav(pcm, sample_rate, sample_width=2):
    """单声道 PCM → 16bit WAV 头"""
    nch, bits = 1, sample_width * 8
    ba = sample_rate * nch * sample_width
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, nch, sample_rate, ba,
                          nch * sample_width, bits)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


def _multipart(wav_bytes):
    """组 multipart/form-data（file 字段），返回 (body, content_type)"""
    boundary = "----voiceime" + uuid.uuid4().hex
    head = ('--%s\r\nContent-Disposition: form-data; name="file"; '
            'filename="speech.wav"\r\nContent-Type: audio/wav\r\n\r\n'
            % boundary).encode()
    tail = ("\r\n--%s--\r\n" % boundary).encode()
    return head + wav_bytes + tail, "multipart/form-data; boundary=" + boundary


class DeepSeekASR:
    """单次录音会话的 DeepSeek 转写客户端（非流式，整段上传）。"""

    is_nonstream = True    # service 据此判断：无握手、无 interim、失败必须提示

    def __init__(self, api_key, endpoint=URL, sample_rate=16000, sample_width=2):
        self.api_key = (api_key or "").strip()
        self.endpoint = endpoint or URL
        self.sample_rate = sample_rate
        self.sample_width = sample_width
        self.finals = []
        self.server_error = None
        self._error = None
        self._buf = bytearray()
        self._on_interim = None
        self._on_final = None
        self._max = min(sample_rate * sample_width * MAX_SEC, MAX_BYTES)

    # ── 生命周期 ──
    def start(self, on_interim=None, on_final=None):
        """无连接可建：只校验 Key 并登记回调（Key 为空立刻报错，走浮窗提示）"""
        if not self.api_key:
            raise RuntimeError("未配置 DeepSeek 语音 API Key（配置台 → 识别引擎）")
        self._on_interim = on_interim
        self._on_final = on_final
        return self

    def send_audio(self, chunk):
        """追加 PCM（Recorder 200ms 节奏）。超 120s 截断，调用方靠切段续录兜底。"""
        room = self._max - len(self._buf)
        if room <= 0:
            if not getattr(self, "_trunc_logged", False):
                self._trunc_logged = True
                log.warning("音频超过 %d 秒上限，丢弃末尾", MAX_SEC)
            return True
        self._buf.extend(chunk[:room])
        return True

    def stop(self, timeout=None):
        """整段上传并返回定稿列表。timeout 被忽略：HTTP 超时按 HTTP_TIMEOUT 计。"""
        pcm = bytes(self._buf)
        self._buf = bytearray()
        if not pcm:
            return self.finals
        try:
            text = self._transcribe(pcm)
        except Exception as e:
            self._error = str(e)
            log.error("DeepSeek 识别失败: %s", e)
            return self.finals
        if text:
            self.finals.append(text)
            if self._on_final:
                self._on_final({"text": text})
        return self.finals

    def close(self):
        self._buf = bytearray()

    # ── 上传 ──
    def _transcribe(self, pcm):
        body, ctype = _multipart(_wav(pcm, self.sample_rate, self.sample_width))
        u = urlsplit(self.endpoint)
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443,
                                           timeout=HTTP_TIMEOUT, context=_ssl_ctx())
        try:
            conn.request("POST", u.path or "/", body=body,
                         headers={"Authorization": "Bearer " + self.api_key,
                                  "Content-Type": ctype,
                                  "Content-Length": str(len(body))})
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
        finally:
            conn.close()
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            obj = {}
        if status != 200:
            err = obj.get("error") if isinstance(obj, dict) else None
            msg = (err or {}).get("message") if isinstance(err, dict) else None
            msg = msg or raw[:200].decode("utf-8", "replace")
            if status in (401, 403):
                raise RuntimeError("DeepSeek 语音 Key 无效（HTTP %d）" % status)
            raise RuntimeError("DeepSeek 识别失败 HTTP %d: %s" % (status, msg))
        return (obj.get("text") or "").strip()


if __name__ == "__main__":
    # 自检（不联网）：WAV 头与 multipart 结构
    pcm = b"\x00\x01" * 16000
    w = _wav(pcm, 16000)
    assert w[:4] == b"RIFF" and w[8:12] == b"WAVE"
    assert struct.unpack("<I", w[40:44])[0] == len(pcm)
    assert struct.unpack("<I", w[24:28])[0] == 16000
    body, ctype = _multipart(w)
    assert b'name="file"' in body and b'filename="speech.wav"' in body
    assert body.startswith(b"--") and body.endswith(b"--\r\n")
    assert ctype.startswith("multipart/form-data; boundary=")
    print("自检通过")
