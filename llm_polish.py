#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DeepSeek 文本润色：核心服务（自动润色）与草稿窗口（「优化」按钮）共用。

配置取自 config.json 的 llm 段（每次调用现读，改动立即生效）：
    endpoint / model / system_prompt / prompt / api_key / auto_polish

api_key 字段：以 sk- 开头视为真实密钥直接使用；否则视为环境变量名，从其取值
（留空默认 DEEPSEEK_API_KEY）——兼容既有配置写法。

请求显式关闭思考模式（thinking.type=disabled）并置 temperature=0（润色要确定性），
使用 JSON Output 模式（response_format.json_object）保证输出可解析：
模型须返回 {"polished": "润色后文本"}，解析取 polished 字段。
（JSON 模式 API 偶发返回空 content → 抛异常，调用方降级为原文。）
"""
import http.client
import json
import os
import re
import ssl
import threading
import urllib.parse
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
DEFAULT_TIMEOUT = 8      # 语音输入场景：内网网关 P95≈1s 但偶发 5-7s，8s 平衡等待与成功率
CONTEXT_N = 3            # 随当前句一起发送的上文原句数


def load_cfg():
    """读 config.json 的 llm 段，展开为「当前生效模型」的扁平配置。

    支持两种写法：
    A) 多模型（配置台推荐）：
        {"auto_polish": false, "active": "zte",
         "models": [{"id","name","endpoint","api_key","model","system_prompt"}, ...]}
       → 返回 active 指向模型的字段合并顶层 auto_polish/prompt。
    B) 旧扁平写法（兼容，逐字透传）：
        {"endpoint":..., "api_key":..., "model":..., ...}
    缺失/异常返回空 dict（调用方按未启用处理）。
    """
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            llm = json.load(f).get("llm") or {}
    except Exception:
        return {}
    models = llm.get("models")
    if not (isinstance(models, list) and models):
        return llm                       # 旧扁平写法：原样返回
    active_id = str(llm.get("active") or "".join(
        dict(models[0]).get("id", "") if isinstance(models[0], dict) else ""))
    m = next((x for x in models
              if isinstance(x, dict) and str(x.get("id")) == active_id), None)
    if m is None:                        # active 指向不存在 → 用第一个，取 id 兜底
        m = models[0]
    m = dict(m) if isinstance(m, dict) else {}
    for k in ("auto_polish", "prompt"):  # 顶层开关/公共字段合入当前模型
        if llm.get(k) is not None:
            m[k] = llm[k]
    return m


def resolve_key(cfg):
    """解析 API Key。规则（配置文件优先，环境变量兜底）：
    1. api_key 留空 → 读环境变量 DEEPSEEK_API_KEY；
    2. api_key 的值恰好是已设置的环境变量名（如 DEEPSEEK_API_KEY）→ 读该变量；
    3. 否则把 api_key 的值本身当密钥用（sk- 开头或内网网关 UUID 均可）。"""
    v = (cfg.get("api_key") or "").strip()
    if not v:
        v = "DEEPSEEK_API_KEY"          # 留空 → 默认环境变量名
    if v in os.environ and os.environ[v].strip():
        return os.environ[v].strip()    # 值是环境变量名且已设置 → 取环境值
    return v                            # 否则视为真实密钥


_ctx = None          # 懒创建的 SSL 上下文（CERT_NONE，内网 MITM 兼容）
_tls = threading.local()   # 每线程连接缓存（各 session 独立 worker 线程）


def _ssl_ctx():
    global _ctx
    if _ctx is None:
        c = ssl.create_default_context()
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        _ctx = c
    return _ctx


def _post(endpoint, body_bytes, timeout, auth=None):
    """HTTP POST（JSON），TLS 连接按 (线程, endpoint) 复用；失效自动重连一次。

    返回响应体 bytes。相比每次新建连接省去每句一次的 TLS 握手（~200ms）。
    """
    u = urllib.parse.urlsplit(endpoint)
    if u.scheme != "https":
        raise RuntimeError("endpoint 仅支持 https: %s" % endpoint)
    cache = getattr(_tls, "conns", None)
    if cache is None:
        cache = _tls.conns = {}
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = "Bearer " + auth
    conn = cache.get(endpoint)
    for attempt in (0, 1):   # 首试 + 断连重建一次
        try:
            if conn is None:
                conn = http.client.HTTPSConnection(u.hostname, u.port or 443,
                                                   timeout=timeout,
                                                   context=_ssl_ctx())
            conn.request("POST", u.path or "/", body=body_bytes, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            if resp.status != 200:
                raise RuntimeError("HTTP %d: %s" % (resp.status, data[:200].decode("utf-8", "replace")))
            cache[endpoint] = conn
            return data
        except (http.client.HTTPException, OSError, ssl.SSLError, RuntimeError) as e:
            try:
                conn.close()
            except Exception:
                pass
            conn = None
            if attempt == 1 or isinstance(e, RuntimeError):
                raise e
    raise RuntimeError("请求失败")   # 不可达（逻辑上不会到这）


def polish(cfg, text, context=None, timeout=None):
    """润色 text（context 为上文原句列表），返回润色后的文本。
    失败抛异常，调用方负责降级为原文。"""
    endpoint = (cfg.get("endpoint") or "").strip()
    if not endpoint:
        raise RuntimeError("未配置 llm.endpoint")
    api_key = resolve_key(cfg)
    if not api_key:
        raise RuntimeError("未找到 API Key（llm.api_key 或环境变量 %s）"
                           % ((cfg.get("api_key") or "").strip() or "DEEPSEEK_API_KEY"))
    # 提示词按「上文/待润色」两段约定；没有上文时也带「待润色：」，让边界约定始终成立
    body_text = (("上文：\n" + "\n".join(context) + "\n") if context else "") + "待润色：\n" + text
    prompt = cfg.get("prompt", "")
    messages = [{"role": "user",
                 "content": (prompt + "\n" + body_text) if prompt else body_text}]
    system_prompt = (cfg.get("system_prompt") or "").strip()
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})
    body = {
        "model": (cfg.get("model") or "").strip(),
        "messages": messages,
        "thinking": {"type": "disabled"},   # v4 默认思考模式（high），润色不需要
        "temperature": 0,
        "response_format": {"type": "json_object"},   # JSON Output：保证可解析
    }
    data = json.loads(_post(endpoint, json.dumps(body).encode("utf-8"),
                            timeout or DEFAULT_TIMEOUT, auth=api_key).decode("utf-8"))
    raw = (data["choices"][0]["message"]["content"] or "").strip()
    if not raw:
        raise RuntimeError("模型返回空 content（JSON 模式偶发）")
    try:
        obj = json.loads(raw)
    except Exception:
        # 容错：旧提示词下模型仍输出 <output> 包裹/纯文本 → 兼容提取
        m = re.search(r"<output>(.*?)</output>", raw, re.S)
        obj = {"polished": (m.group(1).strip() if m else raw)}
    # 字段兼容：官方 DeepSeek 返回 polished；内网网关返回 output
    out = ""
    if isinstance(obj, dict):
        out = (obj.get("polished") or obj.get("output") or "").strip()
    if not out and isinstance(obj, str):
        out = obj.strip()
    if not out:
        raise RuntimeError("模型返回 JSON 缺少 polished/output 字段")
    return out


if __name__ == "__main__":
    # 自检：python3 llm_polish.py "要润色的文本"
    import sys
    c = load_cfg()
    print("endpoint=%s model=%s auto_polish=%s key=%s"
          % (c.get("endpoint"), c.get("model"), c.get("auto_polish"),
             "已配置" if resolve_key(c) else "缺失"))
    if len(sys.argv) > 1:
        print("结果:", polish(c, sys.argv[1]))
