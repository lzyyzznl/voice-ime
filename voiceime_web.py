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

# 顶层允许键 + 类型约束（None = 任意 json 值）
ALLOWED = {
    "asr_backend": str, "app_id": str, "source": str, "user_id": str,
    "secret": str, "env": str,
    "sample_rate": int, "chunk_ms": int, "audio_device": str,
    "silence_timeout_sec": (int, type(None)), "silence_after_input_sec": (int, type(None)),
    "silence_rms_threshold": int, "focus_lost_stop": bool, "hold_ms": int,
}
SUBOBJECTS = {"osd", "llm", "doubao"}   # dict 子对象整体校验类型
BACKENDS = ("doubao", "icenter")


def load_cfg():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def save_cfg(cfg):
    """原子写盘；先备份当前配置到 .bak"""
    try:
        BACKUP.write_text(json.dumps(load_cfg(), ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    tmp = CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(CONFIG)


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
                if isinstance(v.get("request"), dict):
                    d["request"] = {kk: vv for kk, vv in v["request"].items()
                                    if isinstance(vv, (bool, int, str))}
                clean[k] = d
            elif k == "llm":
                clean[k] = {kk: vv for kk, vv in v.items()
                            if kk in ("endpoint", "api_key", "model", "system_prompt", "prompt")
                            and isinstance(vv, str)}
            else:  # osd
                clean[k] = {kk: vv for kk, vv in v.items()
                            if kk in ("width", "height", "y_ratio") and isinstance(vv, (int, float))}
        else:
            errors.append(f"未知键: {k}")
    if "asr_backend" in clean and clean["asr_backend"] not in BACKENDS:
        errors.append("asr_backend 必须为 doubao 或 icenter")
    return clean, errors


def service_status():
    """ASR 核心存活（systemd）+ 引擎连接（socket ESTAB）+ 后端"""
    out = {"alive": False, "pid": None, "backend": None, "engine": False}
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
    try:
        out["backend"] = load_cfg().get("asr_backend")
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
.is-off{filter:grayscale(1);opacity:.55}
.backends{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.rb{position:relative}
.rb input{position:absolute;inset:0;opacity:0;cursor:pointer;z-index:1}
.rb .opt{display:block;height:100%;border:2px solid var(--ink);background:var(--paper2);
  padding:12px 14px;box-shadow:3px 3px 0 var(--ink)}
.rb .opt .t{display:block;font:800 14px/1.2 var(--mono)}
.rb .opt .d{display:block;margin-top:4px;font:11px/1.5 var(--mono);color:var(--muted)}
.rb input:hover+.opt{background:var(--paper)}
.rb input:focus-visible+.opt{outline:3px solid var(--red);outline-offset:3px}
.rb input:checked+.opt{background:var(--ink);color:var(--paper);box-shadow:5px 5px 0 var(--red)}
.rb input:checked+.opt .t{color:var(--paper)}
.rb input:checked+.opt .d{color:rgba(242,239,232,.7)}
.rb input:checked+.opt::after{content:"";position:absolute;top:5px;right:5px;width:10px;height:10px;background:var(--red)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:15px 20px}
.f{display:flex;flex-direction:column;gap:5px;min-width:0}
.f.wide{grid-column:1/-1}
.f label{font:11px/1 var(--mono);color:var(--ink2);letter-spacing:.04em}
input,select,textarea{width:100%;background:var(--paper);border:2px solid var(--ink);color:var(--ink);
  padding:7px 10px;font:inherit;font-size:13px;border-radius:0}
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
.subgrid{display:grid;grid-template-columns:1fr 1fr;gap:15px 20px}
@media(max-width:760px){.subgrid,.llm-grid{grid-template-columns:1fr}.backends{grid-template-columns:1fr}}
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
      <div class="sub">fcitx4 语音输入 · 豆包 / iCenter 流式识别 · 配置保存即写 config.json</div>
    </div>
    <div class="badges" id="badges">
      <span class="badge">后端 <b id="bdgBackend">…</b></span>
      <span class="badge"><span class="dot" id="bdgSvc"></span>服务 <b id="bdgPid">…</b></span>
      <span class="badge"><span class="dot" id="bdgEng"></span>引擎</span>
    </div>
  </header>

  <section class="card">
    <div class="card-h"><h2>识别后端</h2><span class="hint">RESTART 生效</span></div>
    <p class="card-desc">引擎重启后按此配置连接 ASR 服务；切换无需卸载引擎。</p>
    <div class="backends">
      <label class="rb"><input type="radio" name="asr_backend" value="doubao" id="rbDoubao">
        <span class="opt"><span class="t">豆包 SeedASR 2.0</span><span class="d">云端流式 · 首字快 · 停顿定稿（推荐）</span></span></label>
      <label class="rb"><input type="radio" name="asr_backend" value="icenter" id="rbIcenter">
        <span class="opt"><span class="t">iCenter 内网流式</span><span class="d">内网闭环 · 首字约 4s · 凭据已保留</span></span></label>
    </div>
  </section>

  <section class="card" id="doubaoCard">
    <div class="card-h"><h2>豆包 SeedASR</h2><span class="hint" id="doubaoState">识别请求参数</span></div>
    <p class="card-desc">参数随每次识别请求下发；停顿定稿 / 首字加速按毫秒级生效。</p>
    <div class="grid">
      <div class="f"><label for="api_key">API Key</label>
        <div class="pw"><input type="password" id="api_key" autocomplete="off" spellcheck="false"><button class="eye" id="eyeKey" type="button">显示</button></div></div>
      <div class="f"><label for="resource_id">Resource ID（计费项）</label>
        <select id="resource_id">
          <option value="volc.seedasr.sauc.duration">2.0 · 时长版 volc.seedasr.sauc.duration</option>
          <option value="volc.seedasr.sauc.concurrent">2.0 · 并发版 volc.seedasr.sauc.concurrent</option>
          <option value="volc.bigasr.sauc.duration">1.0 · 时长版 volc.bigasr.sauc.duration</option>
          <option value="volc.bigasr.sauc.concurrent">1.0 · 并发版 volc.bigasr.sauc.concurrent</option>
        </select></div>
      <div class="f"><label for="url">流式端点</label>
        <select id="url">
          <option value="nostream">单向流式 bigmodel_nostream（整句、准确率更优，推荐）</option>
          <option value="">双向流式 bigmodel（实时预览、准确率较低）</option>
        </select></div>
    </div>
    <p class="card-desc" style="margin:16px 0 10px">识别参数<span style="color:var(--faint)"> · 默认值即豆包文档推荐，一般无需改</span></p>
    <div class="subgrid">
      <label class="sw"><input type="checkbox" id="enable_punc"><span class="tr"></span><span class="n">标点</span><span class="d">enable_punc</span></label>
      <label class="sw"><input type="checkbox" id="enable_itn"><span class="tr"></span><span class="n">数字书面化</span><span class="d">enable_itn</span></label>
      <label class="sw"><input type="checkbox" id="enable_ddc"><span class="tr"></span><span class="n">语义顺滑 DDC</span><span class="d">enable_ddc</span></label>
      <label class="sw"><input type="checkbox" id="enable_accelerate_text"><span class="tr"></span><span class="n">首字加速</span><span class="d">enable_accelerate_text</span></label>
      <label class="sw"><input type="checkbox" id="enable_nonstream"><span class="tr"></span><span class="n">二遍定稿</span><span class="d">enable_nonstream</span></label>
      <label class="sw"><input type="checkbox" id="show_utterances"><span class="tr"></span><span class="n">输出分段</span><span class="d">show_utterances</span></label>
    </div>
    <div class="grid" style="margin-top:14px">
      <div class="f in-mono"><label for="end_window_size">停顿判停 ms</label><input type="number" id="end_window_size" min="300" max="5000" step="100"></div>
      <div class="f in-mono"><label for="force_to_speech_time">起音保护 ms</label><input type="number" id="force_to_speech_time" min="0" max="5000" step="100"></div>
      <div class="f in-mono"><label for="accelerate_score">加速率 score</label><input type="number" id="accelerate_score" min="0" max="10"></div>
    </div>
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
    <div class="card-h"><h2>LLM 润色</h2><span class="hint">草稿窗「优化」按钮</span></div>
    <p class="card-desc">服务端直连该 LLM 对转写文本做润色；API Key 字段填环境变量名。</p>
    <div class="llm-grid">
      <div class="f"><label for="llm_endpoint">Endpoint</label><input type="text" id="llm_endpoint" spellcheck="false"></div>
      <div class="f"><label for="llm_model">模型</label><input type="text" id="llm_model" spellcheck="false"></div>
      <div class="f"><label for="llm_api_key">API Key（环境变量名）</label><input type="text" id="llm_api_key" spellcheck="false"></div>
    </div>
    <div class="f" style="margin-top:14px"><label for="llm_system_prompt">System Prompt</label>
      <textarea id="llm_system_prompt" spellcheck="false"></textarea></div>
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

</div>

<div class="bar"><div class="bar-in">
  <span class="note" id="barNote">config.json · 上次操作后已备份 .bak</span>
  <button class="btn ghost" id="btnRollback">回滚上次配置</button>
  <button class="btn" id="btnSave">仅保存</button>
  <button class="btn primary" id="btnSaveRestart">保存并重启</button>
</div></div>

<div id="toasts"></div>

<script>
"use strict";
const $ = id => document.getElementById(id);
const setPath = (o, p, v) => { const k = p.split("."); let t = o;
  for (let i = 0; i < k.length - 1; i++) { if (t[k[i]] == null) t[k[i]] = {}; t = t[k[i]]; }
  t[k[k.length-1]] = v; };
const getPath = (o, p) => { let t = o; for (const k of p.split(".")) { if (t == null) return undefined; t = t[k]; } return t; };

/* 字段 → config 路径 + 类型（s=字符串 n=数字 b=布尔） */
const FIELD = [
  ["asr_backend",  "asr_backend", "s"],
  ["api_key",      "doubao.api_key", "s"],
  ["resource_id",  "doubao.resource_id", "s"],
  ["url",          "doubao.url", "s"],
  ["enable_punc",  "doubao.request.enable_punc", "b"],
  ["enable_itn",   "doubao.request.enable_itn", "b"],
  ["enable_ddc",   "doubao.request.enable_ddc", "b"],
  ["enable_accelerate_text", "doubao.request.enable_accelerate_text", "b"],
  ["enable_nonstream", "doubao.request.enable_nonstream", "b"],
  ["show_utterances", "doubao.request.show_utterances", "b"],
  ["end_window_size", "doubao.request.end_window_size", "n"],
  ["force_to_speech_time", "doubao.request.force_to_speech_time", "n"],
  ["accelerate_score", "doubao.request.accelerate_score", "n"],
  ["audio_device", "audio_device", "s", true],
  ["sample_rate",  "sample_rate", "n", true],
  ["chunk_ms",     "chunk_ms", "n", true],
  ["hold_ms",      "hold_ms", "n", true],
  ["silence_timeout_sec", "silence_timeout_sec", "n", true],
  ["silence_after_input_sec", "silence_after_input_sec", "n", true],
  ["silence_rms_threshold", "silence_rms_threshold", "n", true],
  ["focus_lost_stop", "focus_lost_stop", "b", true],
  ["llm_endpoint", "llm.endpoint", "s"],
  ["llm_api_key",  "llm.api_key", "s"],
  ["llm_model",    "llm.model", "s"],
  ["llm_system_prompt", "llm.system_prompt", "s"],
];
const FALLBACK = { enable_punc:true, enable_itn:true, enable_ddc:false,
  enable_accelerate_text:false, enable_nonstream:true, show_utterances:true,
  end_window_size:800, force_to_speech_time:1000, accelerate_score:0 };

function fillForm(cfg){
  for (const [id, path, type, ro] of FIELD){
    let v = getPath(cfg, path);
    if (v === undefined && path in FALLBACK) v = FALLBACK[path];
    if (v === undefined) continue;
    if (id === "asr_backend") {
      document.querySelectorAll('input[name="asr_backend"]')
        .forEach(r => r.checked = (r.value === String(v)));
      continue;
    }
    const el = $(id); if (!el) continue;
    if (ro) { el.textContent = type === "b" ? (v ? "开" : "关") : String(v); continue; }
    if (type === "b") el.checked = !!v;
    else if (type === "n") el.value = v == null ? "" : v;
    else el.value = v;
  }
}
function collectCfg(){
  const cfg = {};
  for (const [id, path, type, ro] of FIELD){
    if (ro) continue;
    if (id === "asr_backend") {
      const r = document.querySelector('input[name="asr_backend"]:checked');
      if (r) setPath(cfg, path, r.value);
      continue;
    }
    const el = $(id); if (!el) continue;
    let v;
    if (type === "b") v = el.checked;
    else if (type === "n") v = el.value === "" ? 0 : Number(el.value);
    else v = el.value;
    setPath(cfg, path, v);
  }
  return cfg;
}

/* ── 状态徽章 ─────────────────────────── */
let state = null;
function setDot(id, cls){ const d = $(id); d.className = "dot" + (cls ? " " + cls : ""); }
function refreshState(){
  fetch("/api/state").then(r => r.json()).then(s => {
    state = s;
    const svc = s.service || {};
    $("bdgBackend").textContent = svc.backend || "?";
    $("bdgPid").textContent = svc.alive ? "PID " + svc.pid : "已停止";
    setDot("bdgSvc", svc.alive ? "ok" : "bad");
    setDot("bdgEng", svc.engine ? "ok" : "");
    if (s.config && !window._filled){ fillForm(s.config); syncDisabled(); window._filled = true; }
  }).catch(() => { setDot("bdgSvc", "bad"); setDot("bdgEng", ""); });
}
function syncDisabled(){
  const rb = document.querySelector('input[name="asr_backend"]:checked');
  const doubao = rb && rb.value === "doubao";
  const card = $("doubaoCard");
  card.classList.toggle("is-off", !doubao);
  $("doubaoState").textContent = doubao ? "识别请求参数" : "当前后端非豆包 · 参数已冻结";
  card.querySelectorAll("input,select").forEach(el => el.disabled = !doubao);
  $("accelerate_score").disabled = !($("enable_accelerate_text").checked && doubao);
  $("rbDoubao").disabled = false; $("rbIcenter").disabled = false;
}
document.querySelectorAll('input[name="asr_backend"]').forEach(r =>
  r.addEventListener("change", syncDisabled));
$("enable_accelerate_text").addEventListener("change", syncDisabled);

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
  if (saving) return; saving = true;
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

$("btnSave").addEventListener("click", () => save(false));
$("btnSaveRestart").addEventListener("click", () => save(true));
$("btnRollback").addEventListener("click", doRollback);

refreshState(); loadLog();
setInterval(refreshState, 4000);
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
                n = int((q.get("n") or ["100"])[0])
                self._send(200, json.dumps({"log": tail_log(name, n)}))
            else:
                self._send(404, json.dumps({"error": "not found"}))
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}))

    def do_POST(self):
        try:
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
                    cfg.update(clean)
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
