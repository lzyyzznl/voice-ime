#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ASR 流式转写客户端（复用已打通的调用链）
==========================================
- 鉴权：X-App-Key/X-Timestamp/X-Signature（HMAC-SHA256）作为 WebSocket 握手头
- UAC token：X-Auth-Value/X-Emp-No 注入 StartTranscription 消息 header（来自本机 UDS 服务）
- 音频：PCM 16kHz/16bit/单声道，200ms 分片二进制帧
- 结果：TranscriptionResult（is_final 区分中间/最终），TranscriptionCompleted 结束
"""
import hashlib
import hmac
import json
import logging
import re
import ssl
import threading
import time
import urllib.request

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

log = logging.getLogger("asr")

ENVS = {
    "test": "wss://icentermsg.test.zte.com.cn/zte-icenter-msg-asr-ws/",
    "gray": "wss://icentermsg.dt.zte.com.cn/zte-icenter-msg-asr1-ws/",
    "prod": "wss://icentermsg.dt.zte.com.cn/zte-icenter-msg-asr-ws/",
}


# ── UAC 凭证（与 icenter-cli auth.rs 一致）────────────────────
DEFAULT_UAC_APP_ID = "564069206224"   # iCenter 桌面客户端在 UDS 上注册的 appId
UDS_PORTS = (19996, 29273, 5865)


def get_uds_credentials(app_id=DEFAULT_UAC_APP_ID):
    """从本机 UDS 服务取 (工号, UAC token)。appId 可被 config.icenter.uac_app_id 覆盖。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for port in UDS_PORTS:
        try:
            url = f"https://127.0.0.1:{port}/requestToken?appId={app_id}&callback=cb&_=1"
            req = urllib.request.Request(
                url, headers={"Referer": "https://uac.zte.com.cn", "Host": "127.0.0.1"}
            )
            data = urllib.request.urlopen(req, timeout=3, context=ctx).read().decode("utf-8")
            token = re.search(r'APPACCESSTOKEN":"([^"]+)', data)
            empno = re.search(r'ZTEDPGSSOUser":"([^"]+)', data)
            if token and empno:
                return empno.group(1), token.group(1)
        except Exception:
            continue
    return None, None


def build_signature(app_id, source, user_id, ts, secret):
    """HMAC-SHA256 签名，payload = f"{app_id}|{source}|{user_id}|{ts}"。"""
    payload = f"{app_id}|{source}|{user_id}|{ts}"
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def build_auth_headers(app_id, source, user_id, secret):
    """握手鉴权头（X-App-Key 等 + source）。"""
    ts = str(int(time.time()))
    return {
        "source": source,
        "X-App-Key": app_id,
        "X-Timestamp": ts,
        "X-Signature": build_signature(app_id, source, user_id, ts, secret),
        "X-Source": source,
        "X-User-Id": user_id,
    }


class StreamingASR:
    """单次 PTT 会话的流式转写客户端（同步模型，线程安全发送）。"""

    def __init__(self, app_id, source, user_id, secret, env="prod",
                 sample_rate=16000, sample_width=2, url=None, uac_app_id=None):
        self.app_id = app_id
        self.source = source
        self.user_id = user_id
        self.secret = secret
        # url 优先（config.icenter.url 直填端点），否则按 env 取内置默认
        self.url = url or ENVS.get(env, ENVS["prod"])
        self.uac_app_id = uac_app_id or DEFAULT_UAC_APP_ID
        self.sample_rate = sample_rate
        self.sample_width = sample_width
        self.ws = None
        self.session_id = None
        self.finals = []          # 最终结果文本（按序）
        self.interims = []        # 最近中间结果
        self._recv_thread = None
        self._completed = threading.Event()
        self.server_error = None  # (status, message)：服务端明确拒绝（503/鉴权失败），重连无意义
        self._error = None

    # ── 生命周期 ──
    def start(self, on_interim=None, on_final=None):
        """建立连接并发送 StartTranscription。阻塞直到会话建立。"""
        empno, token = get_uds_credentials(self.uac_app_id)
        if not token:
            raise RuntimeError("无法从 UDS 获取 UAC token，请确认 iCenter 客户端已登录")

        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        self.ws = connect(
            self.url,
            additional_headers=build_auth_headers(
                self.app_id, self.source, self.user_id, self.secret),
            ssl=ssl_ctx,
            open_timeout=10,
        )
        start_msg = {
            "header": {
                "name": "StartTranscription",
                **build_auth_headers(self.app_id, self.source, self.user_id, self.secret),
                "X-Auth-Value": token,
                "X-Emp-No": empno,
            },
            "payload": {
                "format": "wav",
                "sample_rate": self.sample_rate,
                "sample_width": self.sample_width,
                "hotwords": [],
                "save_audio_file": True,
                "enable_intermediate_result": True,
            },
        }
        self.ws.send(json.dumps(start_msg, ensure_ascii=False))
        log.debug("StartTranscription 已发送")

        # 接收线程
        self._on_interim = on_interim
        self._on_final = on_final
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()
        return self

    def _recv_loop(self):
        try:
            while True:
                msg = self.ws.recv()
                if isinstance(msg, bytes):
                    continue
                obj = json.loads(msg)
                h = obj.get("header", {})
                name = h.get("name")
                if name == "TranscriptionStarted":
                    self.session_id = h.get("session_id")
                    log.debug("会话建立: %s", self.session_id)
                elif name == "TranscriptionResult":
                    p = obj.get("payload", {})
                    if p.get("is_final"):
                        self.finals.append(p.get("text", ""))
                        if self._on_final:
                            self._on_final(p)
                    else:
                        self.interims.append(p.get("text", ""))
                        if self._on_interim:
                            self._on_interim(p)
                elif name == "TranscriptionCompleted":
                    self._completed.set()
                    break
                elif name == "Error":
                    self.server_error = (h.get("status"), h.get("status_message"))
                    self._error = f"服务端错误: {h.get('status')} {h.get('status_message')}"
                    log.error("%s", self._error)
                    self._completed.set()
                    break
                elif name == "heartbeat":
                    self.ws.send(json.dumps({"type": "pong"}))
        except Exception as e:
            # 主动 close()（早松手对讲机/切换）关闭连接时 recv 会抛 ConnectionClosed，
            # 属正常关闭，不算错误
            if not isinstance(e, ConnectionClosed):
                if not self._completed.is_set():
                    self._error = f"接收线程异常: {e}"
                    log.error("%s", self._error)
            self._completed.set()

    # ── 发送 ──
    def send_audio(self, pcm_chunk: bytes):
        """发送二进制音频帧。返回 False 表示连接已不可用。"""
        if self.ws is None:
            return False
        try:
            self.ws.send(pcm_chunk)
            return True
        except Exception as e:
            self._error = f"音频发送失败: {e}"
            return False

    def stop(self, timeout=15):
        """发送 StopTranscription 并等待 TranscriptionCompleted。返回最终文本列表。"""
        if self.ws is not None:
            try:
                stop_msg = {"header": {"name": "StopTranscription"}}
                if self.session_id:
                    stop_msg["header"]["session_id"] = self.session_id
                self.ws.send(json.dumps(stop_msg))
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
