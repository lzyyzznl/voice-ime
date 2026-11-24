# voice-ime 发版规则

发版指的是把改动同步到全部对外出口：仓库、安装包、说明书 HTML、空间页面。漏掉任何一个都算没发完。

## 版本号

`VERSION` 为 `YYYY.MM.DD.N`，同一天多次发版 N 递增。

## 发版清单

1. **bump `VERSION`。**
2. **打包**（zip 顶层目录必须是 `voice-ime/`；排除 `.git`、`logs`、`__pycache__`、`*.pyc`、`config.json`、`config.json.bak*`、`*.html`；必须含预编译的 `fcitx-engine/fcitx-voiceime.so`）：

   ```bash
   cd /home/0668001050/workspace/voice-ime && VER=$(python3 -c "print(open('VERSION').read().strip())") && rm -rf /tmp/pkg && mkdir -p /tmp/pkg/voice-ime && \
   rsync -a --exclude '.git' --exclude 'logs' --exclude '__pycache__' --exclude '*.pyc' \
     --exclude 'config.json' --exclude 'config.json.bak*' --exclude '*.html' ./ /tmp/pkg/voice-ime/ && \
   cd /tmp/pkg && zip -qr /tmp/voice-ime-$VER.zip voice-ime && md5sum /tmp/voice-ime-$VER.zip
   ```

3. **上传安装包**：`edw upload /tmp/voice-ime-<VER>.zip`，记录 `fileKey` 与 ida 下载链接；`curl` 下载回验 md5 一致。
4. **更新说明书**：改 `2026-09-09~进行中-voice-ime构建安装与排障/voice-ime-语音输入法-安装与使用.md` 里的版本号、下载链接、MD5，然后在该目录执行 `python3 素材/md2html.py` 重新生成 HTML。
5. **更新空间页面（必做，别忘）**：

   ```
   https://i.zte.com.cn/index/ispace/#/space/79f7b7d2858f483ead05a983567cec3f/wiki/page/fbbfd7a8d33f11f1a04403e9285ce15e/view
   ```

   ```bash
   cd "/home/0668001050/workspace/voice-ime/2026-09-09~进行中-voice-ime构建安装与排障" && \
   edw update_article --document "https://i.zte.com.cn/index/ispace/#/space/79f7b7d2858f483ead05a983567cec3f/wiki/page/fbbfd7a8d33f11f1a04403e9285ce15e/view" \
     --html_file "$PWD/voice-ime-语音输入法-安装与使用.html"
   ```

   `--html_file` 是上传模式：整页被替换成该 HTML 的 HtmlIncludeMacro。
6. **取说明书公网链接（可选）**：`edw upload <生成的 html>`，用于聊天里直接转发。
7. **提交并推送**：用 eval `commit` 命名空间提交，然后 `git push origin master`，报告提交号与 `origin/master` 范围。

## 对外出口一览

| 出口 | 载体 |
|---|---|
| 代码仓库 | https://github.com/lzyyzznl/voice-ime |
| 安装包 | ida 链接（fileKey）+ MD5，写在说明书引导词里 |
| 说明书 HTML | 本地 `voice-ime-语音输入法-安装与使用.html`（`素材/md2html.py` 生成） |
| 空间页面 | 上面第 5 步的 i.zte.com.cn 页面 |

## 其它约束

- 提交代码用 eval `commit` 命名空间，禁止 `git add/commit/stash/reset/checkout/restore`；「提交代码」= commit + push。
- 上传文件取可分享链接一律 `edw upload`（ida 域名永久链接）。
- 临时产物放 `/tmp`，用完清理；workspace 根目录不留散落文件。
