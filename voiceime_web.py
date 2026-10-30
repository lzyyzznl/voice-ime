#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
voice-ime 配置 Web 界面（零依赖，127.0.0.1:19530）
====================================================
- GET  /             页面
- GET  /api/state    配置 + 服务状态 + 日志尾部
- POST /api/config   保存配置（JSON: {config: {...}, restart: bool}），写盘前备份 .bak
- POST /api/restart  执行 ./service.sh restart
- GET  /api/log?n=N&name=service|osd|draft|engine
"""
import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
BACKUP = BASE / "config.json.bak"
PORT = 19530
LOGS = {
    "service": "voiceime-service.log",
    "osd": "voiceime-osd.log",
    "draft": "voiceime-draft.log",
    "engine": "voiceime-engine.log",
}
HISTORY = BASE / "logs" / "voiceime-history.jsonl"

# 顶层允许键 + 类型约束（None = 任意 json 值）
ALLOWED = {
    "sample_rate": int, "chunk_ms": int, "audio_device": str,
    "silence_timeout_sec": (int, type(None)), "silence_after_input_sec": (int, type(None)),
    "silence_rms_threshold": int, "focus_lost_stop": bool, "hold_ms": int,
    "ptt_keys": str, "hold_keys": str,
}
SUBOBJECTS = {"osd", "llm", "doubao"}   # dict 子对象整体校验类型

MODS = ("ctrl", "alt", "shift")
COMBO_KEYS = {"space", "tab", "esc", "escape", "`"}


def valid_combo(s):
    """与引擎 parse_combo 同规则：修饰键任意组合 + 最多一个普通键。"""
    if not isinstance(s, str) or not s or len(s) > 40:
        return False
    mods, key = set(), None
    for part in s.split("+"):
        p = part.strip().lower()
        if not p:
            return False
        if p in MODS:
            mods.add(p)
        elif p in COMBO_KEYS or (len(p) == 1 and (p.isalnum() or p == "`")):
            if key is not None:
                return False
            key = p
        else:
            return False
    # 必须带修饰键：裸字母/空格在任意输入框打字都会误触发录音
    return bool(mods)


def load_cfg():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


_CFG_LOCK = threading.Lock()   # ThreadingHTTPServer：并发保存会交错覆盖同一个 .tmp


def save_cfg(cfg):
    """原子写盘；先备份当前配置到 .bak"""
    with _CFG_LOCK:
        try:
            BACKUP.write_text(json.dumps(load_cfg(), ensure_ascii=False, indent=2),
                              encoding="utf-8")
        except Exception:
            pass
        tmp = CONFIG.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(CONFIG)


def deep_merge(dst, src):
    """递归合并：只提交部分字段时保留未提交的键。
    否则旧页面（未含新字段）保存会把新配置项整段抹掉。"""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def sanitize(patch):
    """按白名单过滤并类型检查用户提交的顶层键。返回 (clean, errors)。"""
    clean, errors = {}, []
    for k, v in patch.items():
        if k in ALLOWED:
            t = ALLOWED[k]
            if v is not None and not isinstance(v, t):
                errors.append(f"{k}: 类型应为 {t.__name__}")
            else:
                clean[k] = v
        elif k in SUBOBJECTS:
            if not isinstance(v, dict):
                errors.append(f"{k}: 应为对象")
                continue
            if k == "doubao":
                d = {kk: vv for kk, vv in v.items() if kk in ("api_key", "resource_id", "url", "request")}
                if isinstance(v.get("hotwords"), list):
                    d["hotwords"] = [s for s in (str(x).strip() for x in v["hotwords"]) if s]
                if isinstance(v.get("request"), dict):
                    d["request"] = {kk: vv for kk, vv in v["request"].items()
                                    if isinstance(vv, (bool, int, str))}
                clean[k] = d
            elif k == "llm":
                model_keys = ("id", "name", "endpoint", "api_key", "model", "system_prompt", "prompt")
                d = {"active": v.get("active")} if isinstance(v.get("active"), str) else {}
                if isinstance(v.get("auto_polish"), bool):
                    d["auto_polish"] = v["auto_polish"]
                if isinstance(v.get("models"), list):
                    ms = []
                    for m in v["models"]:
                        if not isinstance(m, dict):
                            continue
                        mm = {kk: vv for kk, vv in m.items()
                              if kk in model_keys and isinstance(vv, str)}
                        if mm.get("id"):
                            ms.append(mm)
                    if ms:
                        d["models"] = ms
                clean[k] = d
            else:  # osd
                clean[k] = {kk: vv for kk, vv in v.items()
                            if kk in ("width", "height", "y_ratio") and isinstance(vv, (int, float))}
        else:
            errors.append(f"未知键: {k}")
    # 数值范围：越界值进 config.json 后会被核心服务拿去做除数/超时/缓冲长度
    RANGES = {
        "sample_rate": (8000, 48000),
        "chunk_ms": (20, 1000),
        "hold_ms": (50, 5000),
        "silence_rms_threshold": (0, 32000),
        "silence_timeout_sec": (0, 3600),
        "silence_after_input_sec": (0, 3600),
    }
    for k, (lo, hi) in RANGES.items():
        v = clean.get(k)
        if v is not None and not (lo <= v <= hi):
            errors.append(f"{k}: 应在 {lo}~{hi} 之间（当前 {v}）")
    for k in ("ptt_keys", "hold_keys"):
        if k in clean and not valid_combo(clean[k]):
            errors.append(f"{k}: 必须包含 ctrl/alt/shift 修饰键，可加一个普通键，如 ctrl+alt 或 ctrl+alt+space")
    if clean.get("ptt_keys") and clean.get("ptt_keys") == clean.get("hold_keys"):
        errors.append("按住说话与按住持续对话不能用同一个组合")
    return clean, errors

def verify_doubao(d):
    """真连一次豆包鉴权：握手成功即视为 Key 可用（无效 Key 在握手阶段就 401）。
    只握手不发音频，正常 1 秒内出结果。"""
    import asr_doubao
    asr = None
    try:
        asr = asr_doubao.DoubaoASR(
            api_key=(d.get("api_key") or "").strip(),
            resource_id=d.get("resource_id") or "volc.seedasr.sauc.duration",
            request=d.get("request"), hotwords=[], sample_rate=16000,
            url=asr_doubao.URL_ASYNC)
        asr.start()
        return True, "Key 可用"
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        if "401" in msg or "unauthor" in low:
            msg = "Key 无效（服务端 401）"
        elif "403" in msg:
            msg = "Key 无权限（服务端 403）"
        elif "timeout" in low or "timed out" in low:
            msg = "连接超时，检查网络或代理"
        return False, msg
    finally:
        if asr is not None:
            try:
                asr.close()
            except Exception:
                pass


def service_status():
    """ASR 核心存活（systemd）+ 引擎连接（socket ESTAB）+ 后端"""
    out = {"alive": False, "pid": None, "engine": False}
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "voiceime-core"],
                           capture_output=True, text=True, timeout=5)
        out["alive"] = r.stdout.strip() == "active"
    except Exception:
        pass
    if out["alive"]:
        try:
            r = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", "voiceime-core"],
                               capture_output=True, text=True, timeout=5)
            pid = r.stdout.strip()
            out["pid"] = int(pid) if pid.isdigit() and int(pid) > 1 else None
        except Exception:
            pass
    try:
        r = subprocess.run(["ss", "-xp"], capture_output=True, text=True, timeout=5)
        out["engine"] = any("voiceime-service.sock" in ln and "ESTAB" in ln
                            for ln in r.stdout.splitlines())
    except Exception:
        pass
    return out


def tail_log(name, n):
    try:
        p = LOGS[name]
    except KeyError:
        return ""
    try:
        lines = (BASE / "logs" / p).read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def tail_history(n):
    """输入历史：core 每上屏一条追加一行 JSON。返回最新在前。"""
    try:
        lines = HISTORY.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    items = []
    for line in lines[-n:]:
        try:
            items.append(json.loads(line))
        except Exception:
            continue
    items.reverse()
    return items


def run_restart():
    r = subprocess.run(["systemctl", "--user", "restart", "voiceime-core"],
                       capture_output=True, text=True, timeout=60, env={
                           **os.environ, "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000")})
    out = (r.stdout + r.stderr).strip()
    if r.returncode:
        return out or f"exit={r.returncode}"
    return out or "voiceime-core 已重启"


def render_page():
    return (
        """
<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>voice-ime 配置台</title>
<style>
:root{
  --paper:#f2efe8; --paper2:#e7e1d4; --ink:#171412; --ink2:#3d3731; --muted:#6f685c;
  --red:#d63b26; --green:#1d7d3d; --amber:#a8600c;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono","Noto Sans Mono CJK SC",monospace;
  --sans:"Noto Sans CJK SC","PingFang SC","Microsoft YaHei",-apple-system,sans-serif;
}
*{box-sizing:border-box;border-radius:0}
html{color-scheme:light}
body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.6 var(--sans);
  background-image:repeating-linear-gradient(0deg,transparent 0 31px,rgba(23,20,18,.05) 31px 32px)}
::selection{background:var(--red);color:var(--paper)}
.wrap{max-width:980px;margin:0 auto;padding:26px 24px 120px}
.head{display:flex;align-items:flex-start;gap:14px;margin:4px 0 28px;padding-bottom:16px;border-bottom:3px solid var(--ink)}
.logo{width:38px;height:38px;flex:none;background:var(--ink);color:var(--paper);
  display:grid;place-items:center;box-shadow:3px 3px 0 var(--red)}
.logo svg{width:20px;height:20px}
.h-t h1{margin:0;font:800 21px/1.25 var(--mono);letter-spacing:.02em}
.h-t .sub{margin-top:4px;font:12px/1.5 var(--mono);color:var(--muted)}
.badges{margin-left:auto;display:flex;gap:10px;flex-wrap:wrap;justify-content:flex-end;padding-top:2px}
.badge{display:inline-flex;align-items:center;gap:7px;height:28px;padding:0 10px;
  font:11.5px/1 var(--mono);background:var(--paper2);border:2px solid var(--ink);box-shadow:3px 3px 0 var(--ink)}
.badge b{font-weight:800}
.dot{width:9px;height:9px;background:var(--ink);flex:none}
.dot.ok{background:var(--green)}
.dot.bad{background:var(--red);animation:blink 1.1s steps(2) infinite}
@keyframes blink{50%{opacity:.2}}
.card{background:var(--paper);border:2px solid var(--ink);box-shadow:5px 5px 0 var(--ink);
  margin-bottom:22px;padding:15px 18px 17px;position:relative}
.card::before{content:"";position:absolute;top:-6px;left:-6px;width:12px;height:12px;background:var(--ink)}
.card-h{display:flex;align-items:baseline;gap:10px;margin-bottom:10px}
.card-h h2{font:800 13px/1.2 var(--mono);letter-spacing:.05em}
.card-h .hint{margin-left:auto;font:10.5px/1 var(--mono);color:var(--muted);padding:3px 6px;border:1px solid var(--muted)}
.card-desc{margin:0 0 14px;font-size:12px;color:var(--muted);line-height:1.55}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:15px 20px}
.f{display:flex;flex-direction:column;gap:5px;min-width:0}
.f.wide{grid-column:1/-1}
.f label{font:11px/1 var(--mono);color:var(--ink2);letter-spacing:.04em}
.f .fa{margin:0;font:10.5px/1.55 var(--mono);color:var(--muted)}
input,select,textarea{width:100%;background:var(--paper);border:2px solid var(--ink);color:var(--ink);
  padding:7px 10px;font:inherit;font-size:13px;border-radius:0}
input:disabled{opacity:.45;background:var(--paper);cursor:not-allowed}
input:hover,select:hover,textarea:hover{border-color:var(--ink)}
input:focus-visible,select:focus-visible,textarea:focus-visible{outline:none;border-color:var(--red);box-shadow:3px 3px 0 var(--red)}
textarea{resize:vertical;min-height:110px;font-family:var(--mono);font-size:12px;line-height:1.7}
select{appearance:none;cursor:pointer;padding-right:32px;
  background-image:linear-gradient(45deg,transparent 50%,var(--ink) 50%),linear-gradient(135deg,var(--ink) 50%,transparent 50%);
  background-position:calc(100% - 18px) 55%,calc(100% - 12px) 55%;background-size:6px 6px;background-repeat:no-repeat}
select option{background:var(--paper)}
.in-mono input{font-family:var(--mono)}
.pw{position:relative}
.pw input{padding-right:60px}
.eye{position:absolute;right:4px;top:50%;transform:translateY(-50%);border:1px solid var(--ink);background:var(--paper2);
  color:var(--ink);cursor:pointer;padding:3px 8px;font:11px/1 var(--mono)}
.eye:hover{background:var(--ink);color:var(--paper)}
.chips{display:flex;flex-wrap:wrap;gap:7px;align-items:center;background:var(--paper);border:2px solid var(--ink);
  padding:8px 9px;min-height:46px;cursor:text}
.chips:focus-within{border-color:var(--red);box-shadow:3px 3px 0 var(--red)}
.chips input,.chips input:hover,.chips input:focus-visible{width:auto;flex:1;min-width:150px;border:none;
  box-shadow:none;background:none;padding:4px 2px;font:12.5px/1.6 var(--mono)}
.chip{display:inline-flex;align-items:center;gap:5px;max-width:100%;min-width:0;background:var(--ink);
  color:var(--paper);font:700 12px/1.35 var(--mono);padding:6px 7px 6px 9px}
.chip .t{min-width:0;overflow-wrap:anywhere;word-break:break-all}
.chip-x{flex:none;background:none;border:none;color:var(--paper);cursor:pointer;font:700 14px/1 var(--mono);
  padding:0 1px;opacity:.65}
.chip-x:hover{opacity:1;color:var(--amber)}
.prompt-btn{display:flex;flex-direction:column;gap:6px;width:100%;text-align:left;cursor:pointer;
  background:var(--paper);border:2px solid var(--ink);padding:10px 12px;font:inherit;color:var(--ink)}
.prompt-btn:hover{border-color:var(--red);box-shadow:3px 3px 0 var(--red)}
.prompt-btn .pt{display:flex;align-items:center;gap:8px;font:700 12px/1 var(--mono)}
.prompt-btn .pt .n{margin-left:auto;font-weight:400;color:var(--muted)}
.prompt-btn .pv{font:11.5px/1.65 var(--mono);color:var(--muted);white-space:pre-wrap;word-break:break-word;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.modal{position:fixed;inset:0;z-index:40;background:rgba(23,20,18,.55);display:flex;
  align-items:center;justify-content:center;padding:24px}
.modal[hidden]{display:none!important}
.modal-box{width:min(920px,100%);max-height:88vh;display:flex;flex-direction:column;gap:12px;
  background:var(--paper);border:2px solid var(--ink);box-shadow:8px 8px 0 var(--ink);padding:16px 18px}
.modal-h{display:flex;align-items:center;gap:12px}
.modal-h h3{margin:0;font:800 13px/1.2 var(--mono);letter-spacing:.04em}
.modal-h .sp{margin-left:auto;font:11px/1 var(--mono);color:var(--muted)}
.modal-box textarea{flex:1;min-height:min(58vh,520px)}
.modal-f{display:flex;align-items:center;gap:10px}
.modal-f .fa{margin:0 auto 0 0;font:11px/1 var(--mono);color:var(--muted)}
.subgrid{display:grid;grid-template-columns:1fr 1fr;gap:15px 20px}
@media(max-width:760px){.subgrid,.llm-grid{grid-template-columns:1fr}}
.sw{display:flex;align-items:center;gap:11px;background:var(--paper2);border:2px solid var(--ink);
  padding:8px 12px;cursor:pointer;position:relative;box-shadow:2px 2px 0 var(--ink)}
.sw input{position:absolute;opacity:0;pointer-events:none}
.sw .n{font:600 12px/1.4 var(--mono)}
.sw .d{font:10px/1.3 var(--mono);color:var(--muted)}
.sw .tr{margin-left:auto;width:36px;height:21px;border:2px solid var(--ink);background:var(--paper);
  position:relative;flex:none}
.sw .tr::after{content:"";position:absolute;left:2px;top:2px;width:13px;height:13px;background:var(--ink)}
.sw input:checked+.tr{background:var(--green)}
.sw input:checked+.tr::after{transform:translateX(15px);background:var(--paper);box-shadow:2px 2px 0 var(--ink)}
.sw input:focus-visible+.tr{outline:3px solid var(--red);outline-offset:3px}
.ro-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:0 24px}
.ro{display:flex;justify-content:space-between;gap:12px;padding:6px 1px;border-bottom:2px solid var(--ink);font:12px/1 var(--mono)}
.ro .k{color:var(--muted)}
.ro .v{font-weight:700}
.log-bar{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.log-bar .st{font:11px/1 var(--mono);color:var(--muted)}
.log-bar .grow{flex:1}
.log-box{background:var(--ink);border:2px solid var(--ink);color:#a7f0a0;
  font:11.5px/1.7 var(--mono);padding:12px 14px;height:300px;overflow:auto;white-space:pre-wrap;word-break:break-all}
.log-box.empty::after{content:"（暂无日志输出）";color:rgba(242,239,232,.45)}
.tabs{display:flex;margin:0 0 24px;border-bottom:3px solid var(--ink);gap:8px}
.tab{border:2px solid var(--ink);border-bottom:none;background:var(--paper2);color:var(--muted);
  font:700 12.5px/1 var(--mono);padding:11px 18px;cursor:pointer;position:relative;top:3px;
  display:inline-flex;align-items:center;gap:8px}
.tab:hover{color:var(--ink)}
.tab.on{background:var(--ink);color:var(--paper);box-shadow:3px -3px 0 var(--red)}
.tab.on:hover{color:var(--paper)}
.tab .cnt{padding:2px 6px;background:var(--red);color:var(--paper);font-size:10.5px;font-weight:700}
.pane[hidden]{display:none!important}
.hist-tools{display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap}
.hist-tools input[type=search]{flex:1;min-width:200px;border:2px solid var(--ink);background:var(--paper);
  color:var(--ink);font:12.5px/1 var(--mono);padding:9px 11px}
.hist-tools input[type=search]:focus{outline:3px solid var(--red);outline-offset:1px}
.hist-wrap{max-height:540px;overflow:auto;border:2px solid var(--ink)}
table.hist{width:100%;border-collapse:collapse;font:12.5px/1.6 var(--mono)}
table.hist th{position:sticky;top:0;z-index:1;background:var(--ink);color:var(--paper);text-align:left;
  padding:8px 10px;font:700 11.5px/1 var(--mono);letter-spacing:.04em}
table.hist td{padding:8px 10px;border-bottom:1px solid rgba(23,20,18,.18);vertical-align:top;word-break:break-word}
table.hist tr:hover td{background:var(--paper2)}
table.hist td.t{white-space:nowrap;color:var(--muted);font-size:11.5px}
table.hist td.o{color:var(--muted)}
table.hist td.x{font-weight:700}
table.hist td.s{white-space:nowrap;font-size:11px}
.tagp,.tagn,.tage{display:inline-block;padding:2px 6px;border:1.5px solid;font-size:10.5px;font-weight:700}
.tagp{color:var(--green);border-color:var(--green)}
.tagn{color:var(--muted);border-color:var(--muted)}
.tage{color:var(--red);border-color:var(--red)}
table.hist mark{background:var(--amber);color:var(--paper);padding:0 2px}
.hist-empty{padding:30px;text-align:center;color:var(--muted);font:12px/1 var(--mono)}
.bar{position:fixed;left:0;right:0;bottom:0;z-index:20;background:var(--paper);border-top:3px solid var(--ink)}
.bar-in{max-width:980px;margin:0 auto;padding:11px 24px;display:flex;align-items:center;gap:10px}
.bar .note{font:11px/1 var(--mono);color:var(--muted);margin-right:auto}
.btn{border:2px solid var(--ink);background:var(--paper2);color:var(--ink);padding:8px 18px;
  font:700 13px/1 var(--mono);cursor:pointer;box-shadow:3px 3px 0 var(--ink)}
.btn:hover{transform:translate(1px,1px);box-shadow:2px 2px 0 var(--ink)}
.btn:active{transform:translate(3px,3px);box-shadow:0 0 0 var(--ink)}
.btn:disabled{opacity:.4;cursor:default;transform:none;box-shadow:none}
.btn.primary{background:var(--red);color:var(--paper);border-color:var(--ink)}
.btn.primary:hover{background:#e8543f;transform:none;box-shadow:4px 4px 0 var(--ink)}
.btn.ghost{background:none;border-color:transparent;box-shadow:none;color:var(--muted)}
.btn.ghost:hover{color:var(--red);border-color:transparent;transform:none;box-shadow:none}
#toasts{position:fixed;right:18px;bottom:76px;z-index:30;display:flex;flex-direction:column;gap:10px;align-items:flex-end}
.toast{background:var(--paper);border:2px solid var(--ink);border-left:6px solid var(--red);color:var(--ink);
  font:12px/1.5 var(--mono);padding:10px 14px;box-shadow:5px 5px 0 var(--ink);
  opacity:0;transform:translateY(8px);transition:opacity .15s,transform .15s;max-width:440px}
.toast.show{opacity:1;transform:none}
.toast.ok{border-left-color:var(--green)}
.toast.err{border-left-color:var(--red)}
.toast .tt{font-weight:800;margin-right:6px}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<div class="wrap">

  <header class="head">
    <div class="logo" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="#f2efe8" stroke-width="1.8" stroke-linecap="square"><rect x="7" y="3" width="10" height="15" rx="0"/><path d="M4 11v1a8 8 0 0 0 16 0v-1M12 20v1.6"/></svg></div>
    <div class="h-t">
      <h1>voice-ime 配置台</h1>
      <div class="sub">fcitx4 语音输入 · 豆包流式识别 · 配置保存即写 config.json</div>
    </div>
    <div class="badges" id="badges">
      <span class="badge"><span class="dot" id="bdgSvc"></span>服务 <b id="bdgPid">…</b></span>
      <span class="badge"><span class="dot" id="bdgEng"></span>引擎</span>
    </div>
  </header>

  <nav class="tabs" role="tablist">
    <button class="tab on" data-tab="cfg" role="tab" aria-selected="true">配置</button>
    <button class="tab" data-tab="hist" role="tab" aria-selected="false">输入历史<span class="cnt" id="histCnt">0</span></button>
  </nav>

  <div class="pane" id="pane-cfg">

  <section class="card" id="doubaoCard">
    <div class="card-h"><h2>豆包 SeedASR</h2><span class="hint" id="doubaoState">识别请求参数</span></div>
    <p class="card-desc">参数随每次识别请求下发；停顿定稿 / 首字加速按毫秒级生效。</p>
    <div class="grid" style="grid-template-columns:1fr">
      <div class="f"><label for="api_key">API Key</label>
        <div class="pw"><input type="password" id="api_key" autocomplete="off" spellcheck="false"><button class="eye" id="eyeKey" type="button">显示</button></div>
        <div style="display:flex;align-items:center;gap:10px;margin-top:8px">
          <button class="btn" id="btnVerify" type="button">校验 Key</button>
          <span class="hint" id="keyState">未校验</span>
        </div></div>
    </div>
    <p class="card-desc" style="margin:12px 0 0">流式端点固定为优化双向流式（<code>bigmodel_async</code>）：逐句定稿上屏、边说边预览，单次会话 300 秒兜底无缝续接。资源按 2.0 时长版。需要改动时改 <code>config.json</code>。</p>
    <p class="card-desc" style="margin:16px 0 10px">识别参数<span style="color:var(--faint)"> · 默认值即豆包文档推荐，一般无需改</span></p>
    <div class="subgrid">
      <label class="sw"><input type="checkbox" id="enable_punc"><span class="tr"></span><span class="n">标点</span><span class="d">enable_punc</span></label>
      <label class="sw"><input type="checkbox" id="enable_itn"><span class="tr"></span><span class="n">数字书面化</span><span class="d">enable_itn</span></label>
      <label class="sw"><input type="checkbox" id="enable_ddc"><span class="tr"></span><span class="n">语义顺滑 DDC</span><span class="d">enable_ddc</span></label>
      <label class="sw"><input type="checkbox" id="enable_accelerate_text"><span class="tr"></span><span class="n">首字加速</span><span class="d">enable_accelerate_text</span></label>
      <label class="sw"><input type="checkbox" id="enable_nonstream"><span class="tr"></span><span class="n">二遍定稿</span><span class="d">enable_nonstream</span></label>
      <label class="sw"><input type="checkbox" id="show_utterances"><span class="tr"></span><span class="n">输出分段</span><span class="d">show_utterances</span></label>
    </div>
    <div class="grid" style="margin-top:14px;align-items:start">
      <div class="f in-mono"><label for="end_window_size">停顿判停 ms</label><input type="number" id="end_window_size" min="300" max="5000" step="100">
        <p class="fa">句末静音持续到这里长就判定这句说完、立刻定稿上屏。<b>调小</b>响应更快，但容易把一口气切成好几句；<b>调大</b>句子更完整，上屏更迟。推荐 500–800，默认 800。</p></div>
      <div class="f in-mono"><label for="force_to_speech_time">起音保护 ms</label><input type="number" id="force_to_speech_time" min="0" max="5000" step="100">
        <p class="fa">开口后这段时间内不判停，防止开头的换气、停顿被误切成两句（只在开口后已有语音时起作用）。推荐 1000；一句话开头习惯停顿较久可调到 1500，短句多可降到 500。</p></div>
      <div class="f in-mono"><label for="accelerate_score">加速率 score</label><input type="number" id="accelerate_score" min="0" max="10">
        <p class="fa">「首字加速」的加速强度，0–10。<b>未勾选首字加速时不生效</b>（输入框置灰）。数值越大首字回来越快，识别精度可能略降。推荐 1–2，默认 0。</p></div>
    </div>
    <p class="card-desc" style="margin:16px 0 8px">热词<span style="color:var(--faint)"> · 谐音听错的词、专业术语、内部系统名、人名</span></p>
    <div class="chips" id="hotwordsBox">
      <input id="hotwordsInput" spellcheck="false" autocomplete="off" placeholder="输入一个词后回车添加，可一次粘贴多个">
    </div>
    <p class="fa" style="margin-top:6px">一份词表走两条链路：识别时随请求发给豆包（音近纠错）；开着「自动润色」时再作为专有名词表，把听成常见词的（如 Coclaw → 可口可乐）改回来。保存后下一句录音即生效。</p>
  </section>

  <section class="card">
    <div class="card-h"><h2>快捷键</h2><span class="hint">保存并重启后生效</span></div>
    <p class="card-desc">点击输入框后<b>直接按下想要的组合键</b>，松开即填入；Esc 取消。修饰键支持 ctrl / alt / shift，末尾可带一个普通键（space、tab 或单个字母数字）。按满「长按触发 ms」才开始录音，防误触。</p>
    <div class="grid">
      <div class="f in-mono"><label for="ptt_keys">按住说话（对讲机）</label>
        <input type="text" id="ptt_keys" spellcheck="false" autocomplete="off" placeholder="点击后按下组合键"></div>
      <div class="f in-mono"><label for="hold_keys">按住持续对话</label>
        <input type="text" id="hold_keys" spellcheck="false" autocomplete="off" placeholder="点击后按下组合键"></div>
    </div>
    <p class="card-desc" style="margin-top:12px">「按住说话」松开即停并上屏；「按住持续对话」触发后可松手继续说，按 Esc 结束并上屏。两个组合不能相同；后者可以以前者开头（如上例 <code>ctrl+alt+space</code>），按下末尾那个键即从对讲机升级为持续模式。</p>
  </section>

  <section class="card">
    <div class="card-h"><h2>采集与判停</h2><span class="hint">只读 · 自动识别配置</span></div>
    <p class="card-desc">录音与判停按识别到的配置自动生效，无需手动调整；保存操作不会改动这些值。</p>
    <div class="ro-list">
      <div class="ro"><span class="k">录音设备</span><span class="v" id="audio_device">—</span></div>
      <div class="ro"><span class="k">采样率 Hz</span><span class="v" id="sample_rate">—</span></div>
      <div class="ro"><span class="k">音频分片 ms</span><span class="v" id="chunk_ms">—</span></div>
      <div class="ro"><span class="k">长按触发 ms</span><span class="v" id="hold_ms">—</span></div>
      <div class="ro"><span class="k">静音自动停止 s</span><span class="v" id="silence_timeout_sec">—</span></div>
      <div class="ro"><span class="k">说出后放宽 s</span><span class="v" id="silence_after_input_sec">—</span></div>
      <div class="ro"><span class="k">静音门限 RMS</span><span class="v" id="silence_rms_threshold">—</span></div>
      <div class="ro"><span class="k">失焦自动停止</span><span class="v" id="focus_lost_stop">—</span></div>
    </div>
  </section>

  <section class="card">
    <div class="card-h"><h2>LLM 润色</h2><span class="hint">草稿窗「优化」+ 自动润色</span></div>
    <p class="card-desc">服务端直连该 LLM 对转写文本做润色；API Key 填 sk- 开头的密钥，或填环境变量名（从环境读取）。可配置多个模型并在下拉中切换（切换即持久化保存）。</p>
    <div class="llm-grid">
      <div class="f"><label for="llm_models">模型</label>
        <select id="llm_models" style="width:100%"></select></div>
      <div class="btns" style="display:flex;gap:8px">
        <button type="button" class="btn ghost" id="btnAddModel" style="flex:1">＋ 新增模型</button>
        <button type="button" class="btn ghost" id="btnDelModel" style="flex:1">删除当前</button>
      </div>
    </div>
    <div class="llm-grid">
      <div class="f"><label for="llm_endpoint">Endpoint</label><input type="text" id="llm_endpoint" spellcheck="false"></div>
      <div class="f"><label for="llm_model">模型</label><input type="text" id="llm_model" spellcheck="false"></div>
      <div class="f"><label for="llm_api_key">API Key</label><input type="text" id="llm_api_key" spellcheck="false"></div>
    </div>
    <div class="f" style="margin-top:10px"><label for="llm_name">模型名称</label><input type="text" id="llm_name" spellcheck="false" placeholder="下拉里显示的名字"></div>
    <label class="sw" style="margin-top:14px"><input type="checkbox" id="llm_auto_polish"><span class="tr"></span><span class="n">自动润色</span><span class="d">每句上屏前先润色（对当前选中模型生效）</span></label>
    <div class="f" style="margin-top:14px"><label>System Prompt</label>
      <button type="button" class="prompt-btn" id="btnPromptEdit">
        <span class="pt">润色提示词<span class="n" id="promptMeta"></span></span>
        <span class="pv" id="promptPeek"></span>
      </button></div>
    <p class="fa" style="margin-top:6px">提示词末尾会自动追加「专有名词表」（内容取自上面的热词，没填热词就不追加）；这里只写你自己的规则。</p>
  </section>

  <section class="card">
    <div class="card-h"><h2>运行日志</h2><span class="hint" id="logMeta">…</span></div>
    <div class="log-bar">
      <select id="logname" style="width:130px" aria-label="日志文件">
        <option value="service">service</option><option value="engine">engine</option>
        <option value="osd">osd</option><option value="draft">draft</option>
      </select>
      <select id="logn" style="width:86px" aria-label="行数">
        <option value="50">50 行</option><option value="150" selected>150 行</option><option value="500">500 行</option>
      </select>
      <span class="st grow" id="logMeta2"></span>
      <span class="st">自动刷新 6s</span>
    </div>
    <div class="log-box empty" id="logbox" tabindex="0" aria-label="日志内容"></div>
  </section>

  </div><!-- /pane-cfg -->

  <div class="pane" id="pane-hist" hidden>
    <section class="card">
      <div class="card-h"><h2>输入历史</h2><span class="hint" id="histMeta">…</span></div>
      <p class="card-desc">每句上屏都会记录：识别原文与最终上屏（润色后）文本。搜索对两者做模糊匹配。</p>
      <div class="hist-tools">
        <input type="search" id="histQ" placeholder="搜索原文 / 润色后文本（空格分词，需全部命中）" aria-label="搜索输入历史">
        <select id="histn" style="width:96px" aria-label="条数">
          <option value="50">50 条</option><option value="100" selected>100 条</option><option value="300">300 条</option>
        </select>
        <button type="button" class="btn ghost" id="btnHistRefresh">刷新</button>
        <span id="histMeta2" style="font:11px/1 var(--mono);color:var(--muted)"></span>
      </div>
      <div class="hist-wrap">
        <table class="hist">
          <thead><tr><th style="width:136px">时间</th><th>识别原文</th><th>上屏文本（润色后）</th><th style="width:96px">状态</th></tr></thead>
          <tbody id="histBody"></tbody>
        </table>
      </div>
    </section>
  </div>
</div>

<div class="bar"><div class="bar-in">
  <span class="note" id="barNote">config.json · 上次操作后已备份 .bak</span>
  <button class="btn ghost" id="btnRollback">回滚上次配置</button>
  <button class="btn" id="btnSave">仅保存</button>
  <button class="btn primary" id="btnSaveRestart">保存并重启</button>
</div></div>

<div id="toasts"></div>
<div class="modal" id="promptModal" hidden>
  <div class="modal-box" role="dialog" aria-modal="true" aria-label="编辑润色提示词">
    <div class="modal-h"><h3>润色提示词</h3><span class="sp" id="promptFor"></span>
      <button type="button" class="btn ghost" id="btnPromptClose">关闭</button></div>
    <textarea id="promptEditor" spellcheck="false"></textarea>
    <div class="modal-f">
      <span class="fa" id="promptCount"></span>
      <button type="button" class="btn" id="btnPromptReset">还原</button>
      <button type="button" class="btn primary" id="btnPromptOk">确定</button>
    </div>
  </div>
</div>

<script>
"use strict";
const $ = id => document.getElementById(id);
const setPath = (o, p, v) => { const k = p.split("."); let t = o;
  for (let i = 0; i < k.length - 1; i++) { if (t[k[i]] == null) t[k[i]] = {}; t = t[k[i]]; }
  t[k[k.length-1]] = v; };
const getPath = (o, p) => { let t = o; for (const k of p.split(".")) { if (t == null) return undefined; t = t[k]; } return t; };

/* 字段 → config 路径 + 类型（s=字符串 n=数字 b=布尔） */
const FIELD = [
  ["api_key",      "doubao.api_key", "s"],
  ["enable_punc",  "doubao.request.enable_punc", "b"],
  ["enable_itn",   "doubao.request.enable_itn", "b"],
  ["enable_ddc",   "doubao.request.enable_ddc", "b"],
  ["enable_accelerate_text", "doubao.request.enable_accelerate_text", "b"],
  ["enable_nonstream", "doubao.request.enable_nonstream", "b"],
  ["show_utterances", "doubao.request.show_utterances", "b"],
  ["end_window_size", "doubao.request.end_window_size", "n"],
  ["force_to_speech_time", "doubao.request.force_to_speech_time", "n"],
  ["accelerate_score", "doubao.request.accelerate_score", "n"],
  ["hotwords", "doubao.hotwords", "chips"],
  ["audio_device", "audio_device", "s", true],
  ["sample_rate",  "sample_rate", "n", true],
  ["chunk_ms",     "chunk_ms", "n", true],
  ["hold_ms",      "hold_ms", "n", true],
  ["ptt_keys",     "ptt_keys", "s"],
  ["hold_keys",    "hold_keys", "s"],
  ["silence_timeout_sec", "silence_timeout_sec", "n", true],
  ["silence_after_input_sec", "silence_after_input_sec", "n", true],
  ["silence_rms_threshold", "silence_rms_threshold", "n", true],
  ["focus_lost_stop", "focus_lost_stop", "b", true],
  ["llm_auto_polish", "llm.auto_polish", "b"],
];
const FALLBACK = { enable_punc:true, enable_itn:true, enable_ddc:false,
  enable_accelerate_text:false, enable_nonstream:true, show_utterances:true,
  end_window_size:800, force_to_speech_time:1000, accelerate_score:0,
  ptt_keys:"ctrl+alt", hold_keys:"ctrl+alt+space" };

let verifiedKey = null;      // 校验通过时输入框里的 Key；与输入框不一致就不许保存
function setKeyState(msg, kind){
  const el = $("keyState");
  if (!el) return;
  el.textContent = msg;
  el.style.color = kind === "ok" ? "var(--green)"
                 : kind === "err" ? "var(--red)"
                 : kind === "warn" ? "var(--amber)" : "";
}

function fillModels(cfg){
  const llm = cfg && cfg.llm || {};
  let ms = Array.isArray(llm.models) ? llm.models : [];
  // 兼容旧扁平写法：把 llm.endpoint 等包成唯一模型
  if (!ms.length && (llm.endpoint || llm.api_key || llm.model)){
    ms = [{ id:"m0", name:"默认", endpoint:llm.endpoint||"", api_key:llm.api_key||"",
            model:llm.model||"", system_prompt:llm.system_prompt||"" }];
  }
  window.__llmModels = ms;
  window.__llmActive = ms.find(m => m.id === llm.active) ? llm.active : (ms[0] && ms[0].id) || "";
  const sel = $("llm_models");
  sel.innerHTML = "";
  ms.forEach(m => { const o = document.createElement("option");
    o.value = m.id; o.textContent = m.name || m.id || "未命名"; sel.appendChild(o); });
  sel.value = window.__llmActive;
  sel.onchange = () => { window.__llmActive = sel.value; fillModelFields(); };
  fillModelFields();
}
function curModel(){
  return (window.__llmModels || []).find(m => m.id === (window.__llmActive || $("llm_models").value));
}
function fillModelFields(){
  const m = curModel() || {};
  $("llm_endpoint").value = m.endpoint || "";
  $("llm_model").value = m.model || "";
  $("llm_api_key").value = m.api_key || "";
  $("llm_name").value = m.name || "";
  promptPreview();
}
function saveModelFields(){
  const m = curModel(); if (!m) return;
  m.endpoint = $("llm_endpoint").value;
  m.model = $("llm_model").value;
  m.api_key = $("llm_api_key").value;
  m.name = $("llm_name").value || m.name || m.id;
  
  const sel = $("llm_models");
  const o = sel.selectedOptions[0]; if (o) o.textContent = m.name;
}
$("btnAddModel").addEventListener("click", () => {
  saveModelFields();
  const id = "m" + Date.now();
  const ms = window.__llmModels || [];
  ms.push({ id:id, name:"新模型", endpoint:"https://api.deepseek.com/v1/chat/completions",
            api_key:"", model:"deepseek-v4-flash", system_prompt:"" });
  window.__llmActive = id;
  const sel = $("llm_models");
  const o = document.createElement("option"); o.value = id; o.textContent = "新模型";
  sel.appendChild(o); sel.value = id;
  fillModelFields(); $("llm_name").focus(); $("llm_name").select();
});
$("btnDelModel").addEventListener("click", () => {
  const ms = window.__llmModels || []; if (!ms.length) return;
  if (!confirm("删除当前选中的模型？")) return;
  saveModelFields();
  const i = ms.findIndex(m => m.id === window.__llmActive);
  if (i >= 0) ms.splice(i, 1);
  window.__llmActive = ms.length ? ms[0].id : "";
  const sel = $("llm_models"); sel.innerHTML = "";
  ms.forEach(m => { const o = document.createElement("option");
    o.value = m.id; o.textContent = m.name || m.id || "未命名"; sel.appendChild(o); });
  sel.value = window.__llmActive;
  fillModelFields();
});
function collectLlm(){
  saveModelFields();
  const ms = window.__llmModels || [];
  return { auto_polish: $("llm_auto_polish").checked,
           active: window.__llmActive || (ms[0] && ms[0].id) || "",
           models: ms };
}

function fillForm(cfg){
  fillModels(cfg);
  for (const [id, path, type, ro] of FIELD){
    let v = getPath(cfg, path);
    if (v === undefined && path in FALLBACK) v = FALLBACK[path];
    if (v === undefined) continue;
    if (type === "chips") { hotwordList = Array.isArray(v) ? v.map(String) : []; renderHotwords(); continue; }
    const el = $(id); if (!el) continue;
    if (ro) { el.textContent = type === "b" ? (v ? "开" : "关") : String(v); continue; }
    if (type === "b") el.checked = !!v;
    else if (type === "n") el.value = v == null ? "" : v;
    else if (type === "l") el.value = Array.isArray(v) ? v.join("\\n") : (v == null ? "" : v);
    else el.value = v == null ? "" : v;      // s：字符串原样回填（漏了这行 Key/快捷键都显示为空）
  }
  // 页面加载时存着的 Key 视为已校验（它就是当前生效的）；改过之后必须重新校验
  verifiedKey = String(getPath(cfg, "doubao.api_key") || "").trim();
  setKeyState(verifiedKey ? "当前保存的 Key（未重新校验）" : "未配置", verifiedKey ? "" : "warn");
}
function collectCfg(){
  const cfg = {};
  for (const [id, path, type, ro] of FIELD){
    if (ro) continue;
    if (type === "chips") { setPath(cfg, path, hotwordList.slice()); continue; }
    const el = $(id); if (!el) continue;
    let v;
    if (type === "b") v = el.checked;
    else if (type === "n") v = el.value === "" ? 0 : Number(el.value);
    else if (type === "l") v = String(el.value).split(/[\\n,，;；]+/).map(s => s.trim()).filter(Boolean);
    else v = el.value;                        // s：漏了这行，改成什么都提交不了（静默 saved）
    setPath(cfg, path, v);
  }
  cfg.llm = collectLlm();
  return cfg;
}

/* ── 状态徽章 ─────────────────────────── */
let state = null;
function setDot(id, cls){ const d = $(id); d.className = "dot" + (cls ? " " + cls : ""); }
function refreshState(){
  fetch("/api/state").then(r => r.json()).then(s => {
    state = s;
    const svc = s.service || {};
    $("bdgPid").textContent = svc.alive ? "PID " + svc.pid : "已停止";
    setDot("bdgSvc", svc.alive ? "ok" : "bad");
    setDot("bdgEng", svc.engine ? "ok" : "");
    if (s.config && !window._filled){ fillForm(s.config); syncDisabled(); window._filled = true; }
  }).catch(() => { setDot("bdgSvc", "bad"); setDot("bdgEng", ""); });
}
function syncDisabled(){
  // 加速评分只在勾选「首字加速」时可填
  $("accelerate_score").disabled = !$("enable_accelerate_text").checked;
}
$("enable_accelerate_text").addEventListener("change", syncDisabled);

/* ── 热词（chips 输入）─────────────────── */
let hotwordList = [];
function renderHotwords(){
  const box = $("hotwordsBox"), inp = $("hotwordsInput");
  if (!box) return;
  box.querySelectorAll(".chip").forEach(c => c.remove());
  hotwordList.forEach(w => {
    const c = document.createElement("span"); c.className = "chip";
    const t = document.createElement("span"); t.className = "t"; t.textContent = w;
    c.appendChild(t);
    const x = document.createElement("button"); x.type = "button"; x.className = "chip-x";
    x.textContent = "×"; x.title = "删除"; x.setAttribute("aria-label", "删除 " + w);
    x.addEventListener("click", () => { hotwordList = hotwordList.filter(v => v !== w); renderHotwords(); });
    c.appendChild(x); box.insertBefore(c, inp);
  });
}
function addHotwords(raw){
  const ws = String(raw).split(/[\\n,，;；、]+/).map(s => s.trim()).filter(Boolean);
  if (!ws.length) return;
  ws.forEach(w => { if (!hotwordList.includes(w)) hotwordList.push(w); });
  renderHotwords();
}
const hotInp = $("hotwordsInput");
hotInp.addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); addHotwords(e.target.value); e.target.value = ""; }
  else if (e.key === "Backspace" && !e.target.value && hotwordList.length) { hotwordList.pop(); renderHotwords(); }
});
/* 失焦即收录，点了「保存」也不会漏掉刚敲的词 */
hotInp.addEventListener("blur", e => { addHotwords(e.target.value); e.target.value = ""; });
hotInp.addEventListener("paste", e => {
  const t = (e.clipboardData || window.clipboardData).getData("text") || "";
  if (/[\\n,，;；、]/.test(t)) { e.preventDefault(); addHotwords(t); }
});
$("hotwordsBox").addEventListener("click", e => { if (e.target === e.currentTarget) hotInp.focus(); });

/* ── 润色提示词弹窗 ────────────────────── */
let promptOrig = "";
function promptPreview(){
  const s = (curModel() || {}).system_prompt || "";
  $("promptPeek").textContent = s || "（空 · 点这里编辑）";
  $("promptMeta").textContent = s ? s.length + " 字" : "未设置";
}
function promptCount(){
  const n = $("promptEditor").value.length;
  $("promptCount").textContent = n ? n + " 字" : "空";
}
$("btnPromptEdit").addEventListener("click", () => {
  saveModelFields();
  const m = curModel() || {};
  promptOrig = m.system_prompt || "";
  $("promptFor").textContent = m.name || m.id || "";
  $("promptEditor").value = promptOrig;
  promptCount();
  $("promptModal").hidden = false;
  $("promptEditor").focus();
});
function closePrompt(){ $("promptModal").hidden = true; }
$("btnPromptClose").addEventListener("click", closePrompt);
$("btnPromptReset").addEventListener("click", () => {
  $("promptEditor").value = promptOrig; promptCount(); $("promptEditor").focus();
});
$("btnPromptOk").addEventListener("click", () => {
  const m = curModel(); if (m) m.system_prompt = $("promptEditor").value;
  saveModelFields(); promptPreview(); closePrompt();
});
$("promptEditor").addEventListener("input", promptCount);
$("promptModal").addEventListener("click", e => { if (e.target === e.currentTarget) closePrompt(); });
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !$("promptModal").hidden) { e.preventDefault(); closePrompt(); }
});

/* ── toast ────────────────────────────── */
function toast(title, msg, kind, ms){
  const t = document.createElement("div");
  t.className = "toast" + (kind ? " " + kind : "");
  t.innerHTML = '<span class="tt"></span><span></span>';
  t.firstChild.textContent = title; t.lastChild.textContent = msg || "";
  $("toasts").appendChild(t);
  requestAnimationFrame(() => t.classList.add("show"));
  setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.remove(), 250); }, ms || 3400);
}

/* ── 保存 / 重启 ──────────────────────── */
async function post(path, body){
  const r = await fetch(path, { method:"POST", headers:{ "Content-Type":"application/json" },
    body: body ? JSON.stringify(body) : undefined });
  return r.json().catch(async () => ({ error: await r.text() }));
}
let saving = false;
async function save(restart){
  if (saving) return;
  if ($("api_key").value.trim() !== verifiedKey){
    toast("需要先校验 API Key", "点「校验 Key」，通过后才能保存", "err", 5200);
    return;
  }
  saving = true;
  const btn = restart ? $("btnSaveRestart") : $("btnSave");
  const old = btn.textContent; btn.disabled = true; btn.textContent = restart ? "保存并重启中…" : "保存中…";
  try {
    const r = await post("/api/config", { config: collectCfg(), restart });
    if (r.error) toast("保存失败", r.error, "err", 5200);
    else { toast(restart ? "已保存并重启" : "已保存", r.msg.replace(/^config\.json 已更新\s*\|?\s*/, ""), restart ? "ok" : undefined);
           if (restart) { toast("重启输出", r.msg, "ok"); setTimeout(loadLog, 800); } }
  } catch(e){ toast("请求失败", String(e), "err"); }
  btn.disabled = false; btn.textContent = old; saving = false;
  refreshState();
}
async function doRestart(){
  const r = await post("/api/restart", {});
  toast(r.error ? "重启失败" : "重启完成", r.msg || r.error, r.error ? "err" : "ok");
  setTimeout(loadLog, 800); refreshState();
}
async function doRollback(){
  if (!confirm("回滚到上次保存的 config.json.bak？当前未保存改动将保留在 config.json 中（被覆盖）。")) return;
  const r = await post("/api/config", { rollback:true, restart:false });
  toast(r.error ? "回滚失败" : "已回滚", r.msg || r.error, r.error ? "err" : "ok");
  window._filled = false; refreshState();
}

/* ── 输入历史（独立 tab）────────────────── */
let histItems = [];
const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const histWords = q => q.trim().toLowerCase().split(" ").filter(Boolean);
/* 模糊搜索命中高亮：按字符标记区间再拼接，避免二次替换破坏已插入的标签 */
function hl(text, ws) {
  if (!ws.length) return esc(text);
  const low = text.toLowerCase(), marks = new Array(text.length).fill(false);
  for (const w of ws) {
    let i = 0;
    while ((i = low.indexOf(w, i)) !== -1) {
      for (let k = i; k < i + w.length && k < text.length; k++) marks[k] = true;
      i += w.length;
    }
  }
  let out = "", open = false;
  for (let i = 0; i < text.length; i++) {
    if (marks[i] !== open) { out += open ? "</mark>" : "<mark>"; open = marks[i]; }
    out += esc(text[i]);
  }
  return open ? out + "</mark>" : out;
}
function renderHist() {
  const ws = histWords($("histQ").value);
  const rows = histItems.filter(it => !ws.length ||
    ws.every(w => ((it.orig || "") + " " + (it.text || "")).toLowerCase().indexOf(w) !== -1));
  $("histCnt").textContent = histItems.length;
  $("histMeta").textContent = ws.length ? "匹配 " + rows.length + " / " + histItems.length + " 条"
                                        : "最近 " + histItems.length + " 条";
  const tb = $("histBody");
  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="4" class="hist-empty">' +
      (ws.length ? "没有匹配的记录" : "（还没有语音输入记录）") + "</td></tr>";
    return;
  }
  tb.innerHTML = rows.map(it => {
    const tag = it.fallback ? '<span class="tage">润色失败</span>'
      : it.polished ? '<span class="tagp">润色 ' + it.ms + "ms</span>"
      : '<span class="tagn">原文</span>';
    const t = String(it.ts || "");
    return "<tr>" +
      '<td class="t" title="' + esc(t) + '">' + esc(t.slice(11) || t) + "</td>" +
      '<td class="o">' + hl(it.orig || "", ws) + "</td>" +
      '<td class="x">' + hl(it.text || "", ws) + "</td>" +
      '<td class="s">' + tag + "</td></tr>";
  }).join("");
}
async function loadHist() {
  const r = await fetch("/api/history?n=" + $("histn").value)
    .then(r => r.json()).catch(() => ({ items: [] }));
  histItems = r.items || [];
  $("histMeta2").textContent = "更新 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
  renderHist();
}
function showTab(name) {
  for (const el of document.querySelectorAll(".pane")) el.hidden = el.id !== "pane-" + name;
  for (const b of document.querySelectorAll(".tab")) {
    const on = b.dataset.tab === name;
    b.classList.toggle("on", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  }
  if (name === "hist") loadHist();
}
document.querySelectorAll(".tab").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));
$("histQ").addEventListener("input", renderHist);
$("histn").addEventListener("change", loadHist);
$("btnHistRefresh").addEventListener("click", loadHist);

/* ── 日志 ─────────────────────────────── */
let atBottom = true;
function scrollFollow(){ const b = $("logbox"); atBottom = b.scrollTop + b.clientHeight >= b.scrollHeight - 90; }
async function loadLog(){
  const name = $("logname").value, n = $("logn").value;
  const r = await fetch("/api/log?name=" + name + "&n=" + n).then(r => r.json()).catch(() => ({ error: "日志读取失败" }));
  const box = $("logbox");
  const shouldStick = atBottom;
  box.textContent = r.log || "";
  box.classList.toggle("empty", !r.log);
  $("logMeta").textContent = name + ".log · 尾部 " + n + " 行";
  $("logMeta2").textContent = r.log ? "更新 " + new Date().toLocaleTimeString("zh-CN", {hour12:false}) : "";
  if (shouldStick) box.scrollTop = box.scrollHeight;
}
$("logbox").addEventListener("scroll", scrollFollow);
$("logname").addEventListener("change", loadLog);
$("logn").addEventListener("change", loadLog);

/* ── 密码显示 ─────────────────────────── */
$("eyeKey").addEventListener("click", () => {
  const i = $("api_key"), show = i.type === "password";
  i.type = show ? "text" : "password";
  $("eyeKey").textContent = show ? "隐藏" : "显示";
});

/* ── Key 校验：不通过不许保存 ─────────────── */
$("btnVerify").addEventListener("click", async () => {
  const btn = $("btnVerify"), old = btn.textContent;
  btn.disabled = true; btn.textContent = "校验中…";
  setKeyState("正在连接豆包…");
  try {
    const cfg = collectCfg();
    const r = await post("/api/verify", { doubao: cfg.doubao });
    if (r.ok) { verifiedKey = $("api_key").value.trim(); setKeyState(r.msg || "Key 可用", "ok"); }
    else { verifiedKey = null; setKeyState(r.msg || r.error || "校验失败", "err"); }
  } catch (e) {
    verifiedKey = null; setKeyState("请求失败：" + e, "err");
  }
  btn.disabled = false; btn.textContent = old;
});
$("api_key").addEventListener("input", () => {
  if ($("api_key").value.trim() !== verifiedKey)
    setKeyState("已修改，保存前需要重新校验", "warn");
});

/* ── 快捷键捕获：聚焦后直接按组合键 ────── */
const MODKEY = { Control:"ctrl", Alt:"alt", Shift:"shift" };
const PLAINKEY = { " ":"space", Tab:"tab" };
for (const ch of "abcdefghijklmnopqrstuvwxyz0123456789") PLAINKEY[ch] = ch;
for (const ch of "ABCDEFGHIJKLMNOPQRSTUVWXYZ") PLAINKEY[ch] = ch.toLowerCase();

function bindHotkey(id){
  const el = $(id); if (!el) return;
  const PH = el.placeholder;
  let mods = new Set(), keyName = "", orig = "", last = "";
  /* soft=true 只刷新显示，不更新 last（松手时不能把已按过的修饰键抹掉） */
  const render = soft => {
    el.value = [...mods, keyName].filter(Boolean).join("+");
    if (!soft) last = el.value;
  };
  el.addEventListener("focus", () => {
    orig = el.value; mods.clear(); keyName = ""; last = "";
    el.value = "";                      /* 清空显示，明确进入捕获态 */
    el.placeholder = "按下组合键…（Esc 取消）";
  });
  el.addEventListener("blur", () => {
    el.placeholder = PH;
    if (!el.value) el.value = orig;     /* 没按任何键就离开 → 恢复原值 */
  });
  el.addEventListener("keydown", e => {
    if (e.key === "Escape") { el.value = orig; el.blur(); return; }
    const m = MODKEY[e.key];
    if (m) { e.preventDefault(); mods.add(m); render(); return; }
    const k = PLAINKEY[e.key];
    if (!k) return;              /* 不支持的键：忽略，继续等待 */
    e.preventDefault(); e.stopPropagation();
    if (!mods.size) {            /* 裸键会跟打字冲突：提示并继续等待 */
      el.placeholder = "需要至少一个修饰键（ctrl / alt / shift）";
      return;
    }
    keyName = k; render(); el.blur();
  });
  el.addEventListener("keyup", e => {
    const m = MODKEY[e.key];
    if (!m || keyName) return;
    e.preventDefault();
    const done = last;
    mods.delete(m);
    if (!mods.size) { el.value = done || orig; el.blur(); }
    else render(true);
  });
}
bindHotkey("ptt_keys"); bindHotkey("hold_keys");

$("btnSave").addEventListener("click", () => save(false));
$("btnSaveRestart").addEventListener("click", () => save(true));
$("btnRollback").addEventListener("click", doRollback);

refreshState(); loadLog(); loadHist();
setInterval(refreshState, 4000);
setInterval(() => { if (!$("pane-hist").hidden) loadHist(); }, 6000);
setInterval(loadLog, 6000);
</script></body></html>
"""
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 页面/接口都不缓存：否则改完 JS 用户还跑旧页面（旧页面没有 Key 校验，照样能存进无效 key）
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            if self.path == "/" or self.path.startswith("/?"):
                self._send(200, render_page(), "text/html; charset=utf-8")
            elif self.path.startswith("/api/state"):
                cfg = load_cfg()
                self._send(200, json.dumps({"config": cfg,
                                            "service": service_status(),
                                            "save_time": (BACKUP.stat().st_mtime if BACKUP.exists() else 0)}))
            elif self.path.startswith("/api/log"):
                import urllib.parse
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                name = (q.get("name") or ["service"])[0]
                n = min(max(int((q.get("n") or ["100"])[0]), 1), 2000)
                self._send(200, json.dumps({"log": tail_log(name, n)}))
            elif self.path.startswith("/api/history"):
                import urllib.parse
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                n = min(max(int((q.get("n") or ["100"])[0]), 1), 500)
                self._send(200, json.dumps({"items": tail_history(n)}))
            else:
                self._send(404, json.dumps({"error": "not found"}))
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}))

    def _cross_site(self):
        """本机浏览器里任意网页都能 POST 到 19530（no-cors 即可），只放行配置台自己的来源"""
        origin = self.headers.get("Origin") or ""
        if not origin:
            return False
        return not origin.startswith(("http://127.0.0.1:%d" % PORT,
                                      "http://localhost:%d" % PORT))

    def do_POST(self):
        try:
            if self._cross_site():
                self._send(403, json.dumps({"error": "跨站请求已拒绝"}))
                return
            if self.path == "/api/verify":
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                d = body.get("doubao") or {}
                if not (d.get("api_key") or "").strip():
                    self._send(400, json.dumps({"ok": False, "msg": "API Key 为空"},
                                               ensure_ascii=False))
                    return
                ok, msg = verify_doubao(d)
                self._send(200 if ok else 400,
                           json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False))
                return
            if self.path == "/api/config":
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                if body.get("rollback"):
                    if not BACKUP.exists():
                        self._send(400, json.dumps({"error": "无备份"}))
                        return
                    BACKUP.replace(CONFIG)
                    msg = "已回滚 config.json.bak"
                else:
                    clean, errors = sanitize(body.get("config", {}))
                    if errors:
                        self._send(400, json.dumps({"error": "; ".join(errors)}))
                        return
                    cfg = load_cfg()
                    deep_merge(cfg, clean)
                    save_cfg(cfg)
                    msg = "config.json 已更新"
                if body.get("restart"):
                    msg += " | " + run_restart()
                self._send(200, json.dumps({"msg": msg}))
            elif self.path == "/api/restart":
                self._send(200, json.dumps({"msg": run_restart()}))
            else:
                self._send(404, json.dumps({"error": "not found"}))
        except Exception as e:
            self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}))


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"voice-ime web 配置台: http://127.0.0.1:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
