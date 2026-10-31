#!/usr/bin/env python3
"""把 Obsidian 笔记（本项目的说明书 md）转成自包含的单文件 HTML。

用法：python3 素材/md2html.py
输入：同级目录的 voice-ime-语音输入法-安装与使用.md
输出：voice-ime-语音输入法-安装与使用.html（图片 base64 内嵌，无脚本、无外链）

只覆盖该笔记用到的语法子集：标题 / 段落 / 无序·有序列表 / 表格 / 围栏代码块 /
图片嵌入 ![[a.png|w]] / wikilink / 行内链接 [文字](url) / 引用块与 callout / 行内 **粗体** 与 `代码`。
"""
import base64
import html
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "voice-ime-语音输入法-安装与使用.md"
OUT = ROOT / "voice-ime-语音输入法-安装与使用.html"
ASSETS = ROOT / "assets"


def front_date() -> str:
    """页眉日期取笔记 frontmatter 的 date，不写死。"""
    m = re.match(r"---\n(.*?)\n---", SRC.read_text(encoding="utf-8"), re.S)
    d = re.search(r"^date:\s*(\S+)", m.group(1) if m else "", re.M)
    return d.group(1) if d else ""


def inline(text: str) -> str:
    """行内格式：代码、粗体、外链、wikilink。"""
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                  r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', text)
    text = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]",
                  lambda m: f'<span class="wikilink">{m.group(2) or m.group(1)}</span>', text)
    return text


_fig_no = 0   # 图片序号：生成 :target 放大用的唯一 id
_vid_no = 0   # 视频序号：同上，供 :target 全屏播放


def embed_image(name: str, width: str | None) -> str:
    global _fig_no
    path = ASSETS / name
    if not path.exists():
        return f'<p class="missing">缺图：{name}</p>'
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif"}.get(path.suffix.lower(), "image/png")
    data = base64.b64encode(path.read_bytes()).decode()
    style = f' style="max-width:{width}px"' if width else ""
    _fig_no += 1
    fid = f"fig{_fig_no}"
    # 点图跳到 #figN → CSS 让该图全屏；点遮罩回到 # 关闭。纯 CSS，无脚本。
    return (f'<a class="zoom" href="#{fid}">'
            f'<img class="fig" id="{fid}" src="data:{mime};base64,{data}"'
            f' alt="{html.escape(name)}"{style}></a>')


def convert(md: str) -> str:
    lines = md.splitlines()
    # 去掉 frontmatter
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                lines = lines[i + 1:]
                break

    out: list[str] = []
    i, n = 0, len(lines)
    in_list = None   # "ul" | "ol"
    sec = 0

    def close_list():
        nonlocal in_list
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None

    while i < n:
        raw = lines[i]
        line = raw.rstrip()
        stripped = line.strip()
        # 折叠容器原样透传（<details>/<summary>），其余行内内容照常渲染
        if re.match(r"^</?(details|summary)\b", stripped):
            close_list()
            out.append(stripped)
            i += 1
            continue
        # 围栏代码块
        fence = re.match(r"^```(\w*)\s*$", stripped)
        if fence:
            close_list()
            lang = fence.group(1)
            body = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            cls = f' class="lang-{lang}"' if lang else ""
            out.append(f"<pre{cls}><code>{html.escape(chr(10).join(body))}</code></pre>")
            continue

        # 空行
        if not stripped:
            close_list()
            i += 1
            continue

        # 图片嵌入（单独成行）
        m = re.fullmatch(r"!\[\[([^\]|]+)(?:\|(\d+))?\]\]", stripped)
        if m:
            close_list()
            out.append(f"<figure>{embed_image(m.group(1), m.group(2))}</figure>")
            i += 1
            continue

        # 标题
        if stripped.startswith("### "):
            close_list()
            out.append(f"<h3>{inline(stripped[4:])}</h3>")
            i += 1
            continue
        if stripped.startswith("## "):
            close_list()
            sec += 1
            sid = f"s{sec}"
            title = inline(stripped[3:])
            out.append(f'<section id="{sid}"><h2><span class="num">{sec:02d}</span>{title}</h2>')
            i += 1
            continue
        if stripped.startswith("# "):
            # 笔记的一级标题已在页面 header 渲染，正文里不再重复
            close_list()
            i += 1
            continue

        # 表格
        if stripped.startswith("|") and i + 1 < n and re.match(r"^\|[\s:|-]+\|$", lines[i + 1].strip()):
            close_list()
            header = [c.strip() for c in stripped.strip("|").split("|")]
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append("<table><thead><tr>" +
                       "".join(f"<th>{inline(c)}</th>" for c in header) +
                       "</tr></thead><tbody>")
            for r in rows:
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            out.append("</tbody></table>")
            continue

        # callout / 引用块
        if stripped.startswith(">"):
            close_list()
            block = []
            while i < n and lines[i].strip().startswith(">"):
                block.append(lines[i].strip()[1:].lstrip())
                i += 1
            head = block[0] if block else ""
            cm = re.match(r"^\[!(\w+)\]\s*(.*)$", head)
            if cm:
                kind, title = cm.group(1), cm.group(2)
                body = "\n".join(block[1:]).strip()
                out.append(f'<div class="callout {kind}"><b>{inline(title)}</b>'
                           f'<p>{inline(body)}</p></div>')
            else:
                inner = "".join(f"<p>{inline(b.strip())}</p>" for b in block if b.strip())
                out.append(f'<div class="quote">{inner}</div>')
            continue

        # 列表
        ul = re.match(r"^[-*] (.+)$", stripped)
        ol = re.match(r"^\d+\. (.+)$", stripped)
        if ul or ol:
            want = "ul" if ul else "ol"
            if in_list != want:
                close_list()
                out.append(f"<{want}>")
                in_list = want
            item = (ul or ol).group(1)
            out.append(f"<li>{inline(item)}</li>")
            i += 1
            continue

        # 视频嵌入 !video[说明](url)：媒体走外链（页面资源依赖里唯一的例外）
        vm = re.match(r"^!video\[([^\]]*)\]\((\S+?)\)$", stripped)
        if vm:
            global _vid_no
            close_list()
            _vid_no += 1
            vid = f"vid{_vid_no}"
            # 点「全屏播放」跳到 #vidN → CSS 把该 video 固定铺满视口；点遮罩回 # 关闭。
            # 用 :target 而不是原生全屏：iCenter 的 iframe 不给 fullscreen 授权，已实测点不动。
            # preload=metadata 只取文件头（素材已 +faststart，秒出首帧）；整段由页尾的
            # VID_JS 取回内存换成 blob:，进度条才能拖，见那里的注释。
            out.append(f'<figure class="vid"><video class="vid" id="{vid}" controls '
                       f'preload="metadata" playsinline src="{html.escape(vm.group(2))}"></video>'
                       f'<figcaption>{inline(vm.group(1))} · '
                       f'<a class="zoom-open" href="#{vid}">全屏播放</a>'
                       f'<span class="vid-note"></span></figcaption></figure>')
            i += 1
            continue

        # 段落
        close_list()
        out.append(f"<p>{inline(stripped)}</p>")
        i += 1

    close_list()
    # 收尾未闭合的 section
    body = "\n".join(out)
    parts = re.split(r'(<section id="s\d+">)', body)
    rebuilt, open_sec = [], False
    for part in parts:
        if part.startswith('<section id='):
            if open_sec:
                rebuilt.append("</section>")
            rebuilt.append(part)
            open_sec = True
        else:
            rebuilt.append(part)
    if open_sec:
        rebuilt.append("</section>")
    return "\n".join(rebuilt)


CSS = """
:root {
  --paper: #F7F6F2; --ink: #111111; --ink-soft: #55524B;
  --rule: rgba(17,17,17,.20); --accent: #002FA7;
  --sans: "Helvetica Neue", Inter, "Noto Sans SC", "Source Han Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
  --mono: "JetBrains Mono", SFMono-Regular, Menlo, Consolas, "Noto Sans Mono CJK SC", monospace;
}
* { box-sizing: border-box; border-radius: 0; }
html { -webkit-text-size-adjust: 100%; scroll-behavior: smooth; }
body {
  margin: 0; background: var(--paper); color: var(--ink);
  font-family: var(--sans); font-size: 16.5px; line-height: 1.75;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1180px; margin: 0 auto; padding: 0 40px; }
header.doc { border-bottom: 1px solid var(--ink); padding: 52px 0 36px; }
.meta {
  display: flex; justify-content: space-between; gap: 16px; flex-wrap: wrap;
  font-family: var(--mono); font-size: 11.5px; letter-spacing: .08em;
  text-transform: uppercase; color: var(--ink-soft); margin-bottom: 26px;
}
h1 { margin: 0; font-size: clamp(30px, 3.6vw, 48px); line-height: 1.02; letter-spacing: -.03em; font-weight: 800; }
.layout { display: grid; grid-template-columns: 200px minmax(0, 1fr); gap: 48px; padding: 0 0 80px; }
nav.toc {
  position: sticky; top: 0; align-self: start; max-height: 100vh; overflow: auto;
  padding: 34px 0; font-family: var(--mono); font-size: 12.5px; line-height: 1.9;
  border-right: 1px solid var(--rule);
}
nav.toc a { color: var(--ink-soft); text-decoration: none; border: 0; display: block; }
nav.toc a:hover { color: var(--accent); }
main { padding: 34px 0; min-width: 0; }
section { border-top: 1px solid var(--ink); padding: 40px 0 8px; scroll-margin-top: 12px; }
section:first-of-type { border-top: 0; padding-top: 8px; }
h2 { margin: 0 0 18px; font-size: clamp(22px, 2.4vw, 30px); letter-spacing: -.02em; font-weight: 800; }
h2 .num {
  font-family: var(--mono); font-size: 12px; font-weight: 400; color: var(--accent);
  letter-spacing: .06em; margin-right: 12px; vertical-align: 3px;
}
h3 { margin: 28px 0 8px; font-size: 17.5px; font-weight: 700; }
p { margin: 0 0 14px; max-width: 46em; text-wrap: pretty; }
ul, ol { margin: 0 0 16px; padding-left: 22px; max-width: 46em; }
li { margin-bottom: 7px; }
a { color: var(--accent); text-decoration: underline; text-underline-offset: 2px; }
code { font-family: var(--mono); font-size: .87em; background: rgba(17,17,17,.05); padding: 1px 4px; }
pre {
  margin: 0 0 18px; padding: 16px 18px; background: var(--ink); color: #F7F6F2;
  font-size: 13px; line-height: 1.8; overflow-x: auto; max-width: 100%;
}
pre code { background: none; padding: 0; font-size: 13px; color: inherit; }
.codewrap { position: relative; margin: 0 0 18px; max-width: 100%; }
.codewrap pre { margin: 0; padding-right: 86px; }
pre.lang-text { white-space: pre-wrap; overflow-wrap: anywhere; }
button.copy {
  position: absolute; top: 8px; right: 8px;
  font-family: var(--mono); font-size: 11px; letter-spacing: .06em;
  padding: 3px 9px; cursor: pointer; color: #F7F6F2;
  background: rgba(247,246,242,.10); border: 1px solid rgba(247,246,242,.45);
  transition: background .15s, border-color .15s, color .15s;
}
button.copy:hover { background: rgba(247,246,242,.22); border-color: #F7F6F2; }
button.copy.ok { color: #9DB8FF; border-color: #9DB8FF; }
@media print { button.copy, a.zoom-close { display: none; } }
table { width: 100%; border-collapse: collapse; font-size: 15px; margin: 0 0 18px; }
th {
  text-align: left; font-family: var(--mono); font-size: 11px; font-weight: 500;
  text-transform: uppercase; letter-spacing: .1em; color: var(--ink-soft);
  border-bottom: 1px solid var(--ink); padding: 0 10px 8px;
}
td { padding: 10px; border-bottom: 1px solid var(--rule); vertical-align: top; }
td:first-child { white-space: nowrap; }
figure { margin: 0 0 20px; }
img.fig { display: block; max-width: 100%; height: auto; border: 1px solid var(--rule); }
a.zoom { display: inline-block; max-width: 100%; cursor: zoom-in; }
a.zoom:hover img.fig { border-color: var(--ink); }
a.zoom-close { display: none; }
img.fig:target {
  position: fixed; inset: 0; margin: auto; z-index: 100;
  width: auto; height: auto;
  max-width: 96vw !important; max-height: 94vh !important;   /* 压过 md 里的宽度内联样式 */
  border: 0; background: var(--paper); cursor: zoom-out;
  box-shadow: 0 0 0 100vmax rgba(247, 246, 242, .985);
}
body:has(img.fig:target) a.zoom-close,
body:has(video.vid:target) a.zoom-close {
  display: block; position: fixed; inset: 0; z-index: 90; cursor: zoom-out;
}
figure.diagram { border: 1px solid var(--rule); padding: 18px; color: var(--ink); background: transparent; }
figure.diagram svg { display: block; width: 100%; height: auto; }
.vid video { display: block; width: 100%; border: 2px solid var(--ink); background: #000; }
.vid figcaption { margin-top: 8px; font-family: var(--mono); font-size: 11.5px; color: var(--muted); }
.zoom-open { cursor: zoom-in; }
video.vid:target {
  position: fixed; inset: 0; margin: auto; z-index: 100;
  width: auto; height: auto;
  max-width: 96vw !important; max-height: 88vh !important;
  border: 0; background: #000; cursor: zoom-out;
  box-shadow: 0 0 0 100vmax rgba(247, 246, 242, .985);
}
.callout { border: 1px solid var(--ink); border-left: 4px solid var(--accent); padding: 12px 16px; margin: 0 0 16px; max-width: 46em; }
.callout b { display: block; font-size: 15px; margin-bottom: 2px; }
.callout p { margin: 0; font-size: 15px; }
.quote { border-left: 3px solid var(--ink); padding: 2px 0 2px 16px; margin: 0 0 18px; color: var(--ink-soft); }
.quote p { margin: 0 0 8px; font-size: 16px; }
.quote strong { color: var(--ink); }
.wikilink { border-bottom: 1px dashed var(--accent); color: var(--accent); }
.missing { color: #B00020; font-family: var(--mono); font-size: 13px; }
footer.doc { border-top: 1px solid var(--ink); padding: 28px 0 60px; font-family: var(--mono); font-size: 11.5px; color: var(--ink-soft); }
@media (max-width: 900px) {
  .wrap { padding: 0 20px; }
  .layout { grid-template-columns: 1fr; gap: 0; }
  nav.toc { position: static; max-height: none; border-right: 0; border-bottom: 1px solid var(--rule); padding: 18px 0; font-size: 12px; }
  nav.toc a { display: inline; margin-right: 12px; }
  body { font-size: 16px; }
  table { display: block; overflow-x: auto; }
}
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }
"""


# 复制按钮：按钮与逻辑全部由脚本注入 —— 无脚本环境（如 Obsidian HTML Reader
# 会剥离 <script>）下页面里根本没有按钮，不会出现点不动的死按钮。
COPY_JS = """
(function () {
  function flash(btn, text) {
    btn.textContent = text; btn.classList.add('ok');
    setTimeout(function () { btn.textContent = '复制'; btn.classList.remove('ok'); }, 1200);
  }
  function manual(btn, pre) {
    var r = document.createRange(); r.selectNodeContents(pre);
    var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    flash(btn, ok ? '已复制' : '按 Ctrl+C');
    setTimeout(function () { sel.removeAllRanges(); }, 1200);
  }
  function grab(btn, pre) {
    var text = pre.innerText.replace(/\s+$/, '');
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { flash(btn, '已复制'); },
                                               function () { manual(btn, pre); });
    } else { manual(btn, pre); }
  }
  document.querySelectorAll('main pre').forEach(function (pre) {
    var wrap = document.createElement('div'); wrap.className = 'codewrap';
    pre.parentNode.insertBefore(wrap, pre); wrap.appendChild(pre);
    var btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'copy'; btn.textContent = '复制';
    btn.addEventListener('click', function () { grab(btn, pre); });
    wrap.appendChild(btn);
  });
})();
"""


# 视频缓存：ida 的下载接口是 chunked、对 Range 只回 200 全量，<video> 拿不到字节区间，
# 进度条完全拖不动。整段取回内存换成 blob: URL 后 seek 全在本地，随便拖。
# CORS 只回显 i.zte.com.cn 一个 Origin（已实测），页面挂在别的域上 fetch 会失败——
# 失败就保持原样，能顺序播、不能拖，不会出现放不了的死视频。
VID_JS = """
(function () {
  function cache(v) {
    var raw = v.getAttribute('src'), abs;
    try { abs = new URL(raw, location.href); } catch (e) { return; }
    if (!/^https?:$/.test(abs.protocol)) return;   // blob:/data: 已经是本地数据,无需缓存
    var url = abs.href;
    var note = v.parentNode.querySelector('.vid-note');
    var setNote = function (t) { if (note) note.textContent = t ? ' · ' + t : ''; };
    setNote('正在缓存');
    fetch(url).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      if (!r.body) return r.blob();
      var total = +(r.headers.get('content-length') || 0);
      var got = 0, chunks = [], reader = r.body.getReader();
      var pump = function () {
        return reader.read().then(function (s) {
          if (s.done) return new Blob(chunks, { type: 'video/mp4' });
          chunks.push(s.value); got += s.value.length;
          setNote(total ? '缓存 ' + Math.round(got / total * 100) + '%'
                        : '缓存 ' + (got / 1048576).toFixed(1) + ' MB');
          return pump();
        });
      };
      return pump();
    }).then(function (blob) {
      var t = v.currentTime, playing = !v.paused;
      v.src = URL.createObjectURL(blob);
      v.load();
      if (t) v.currentTime = t;
      if (playing) v.play().catch(function () {});
      setNote('已缓存，进度条可自由拖动');
      setTimeout(function () { setNote(''); }, 5000);
    }).catch(function () { setNote(''); });
  }
  Array.prototype.forEach.call(document.querySelectorAll('video.vid'), cache);
})();
"""


def main() -> None:
    global _fig_no, _vid_no
    _fig_no = _vid_no = 0
    md = SRC.read_text(encoding="utf-8")
    body = convert(md)
    title = "voice-ime 语音输入法：安装与使用"

    # 目录：从 h2 抽
    toc = []
    for idx, m in enumerate(re.finditer(r'<h2><span class="num">(\d+)</span>(.*?)</h2>', body), 1):
        toc.append(f'<a href="#s{int(m.group(1))}">{m.group(2)}</a>')

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light">
<meta name="upstream" content="领域/开发经验积累/2026-09-09~进行中-voice-ime构建安装与排障/voice-ime-语音输入法-安装与使用.md">
<title>{title}</title>
<style>{CSS}</style>
</head>
<body>
<header class="doc"><div class="wrap">
  <div class="meta"><span>FCITX 4 · MODULE ADDON</span><span>豆包 SeedASR 2.0</span><span>{front_date()}</span></div>
  <h1>{title}</h1>
</div></header>
<div class="wrap"><div class="layout">
<nav class="toc">
{chr(10).join(toc)}
</nav>
<main>
{body}
</main>
</div></div>
<footer class="doc"><div class="wrap">voice-ime · fcitx4 addon · 由同名 md 生成：素材/md2html.py</div></footer>
<a class="zoom-close" href="#" aria-label="关闭放大"></a>
<script>{COPY_JS}</script>
<script>{VID_JS}</script>
</body>
</html>
"""
    OUT.write_text(doc, encoding="utf-8")
    print(f"{OUT.name}: {len(doc)} bytes, {doc.count('<section')} sections, {doc.count('data:image')} images")


if __name__ == "__main__":
    main()
