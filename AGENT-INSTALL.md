# voice-ime 安装提示词（粘贴给 agent）

发布页（iCenter 空间「AI相关」下的子页面）：
https://i.zte.com.cn/index/ispace/#/space/79f7b7d2858f483ead05a983567cec3f/wiki/page/b172526ed2e111f190abd50f90fea4c5/view

换版流程（与 icenter-key 一致）：
1. 重新打包 zip → `edw upload` 拿新 fileKey
2. 回写两处：本文件与 `install-page.html`（各 1 处 fileKey / fileName / MD5）
3. `edw update_article --document "<上面的页面URL>" --html_file "/abs/path/install-page.html" --ai_generated False`
4. 验证 `success: true` 且 `page_height` 变化

# voice-ime 安装提示词（粘贴给 agent）


安装包：`voice-ime-2026.10.28.2.zip`（72 KB，含已编译引擎，无需 gcc / fcitx-devel）

下载链接：

```
https://ida.zte.com.cn/zte-paas-lcap-demobff/zte-paas-lcap-demoapi/v1/app/file/uncheck/encryptDownload?fileKey=9e49f03d-8eb8-446c-ac02-bd042f3cc8fd&fileName=voice-ime-2026.10.28.2.zip&encryptEnable=false&securityLevel=null
```

MD5：`7e754ae4b7ff2e3eab1440ed773fa506`

---

下面这段整段复制给 agent：

---

【任务】
在这台 Linux 机器上安装 voice-ime 语音输入法：fcitx4 插件 + 豆包流式语音识别，说话即出字，不占用输入法位（拼音照常可用）。安装包里的引擎已经编译好，不需要 gcc、make 或 fcitx-devel。请按下面的步骤一步一步执行，每步做完报告结果；某一步失败就停下来说明现象，不要跳过、不要自己换方案。

【第 1 步：确认环境】
这是 Linux + fcitx4 桌面（X11 或 xrdp）。

```
echo $XDG_SESSION_TYPE; command -v fcitx && fcitx-remote; python3 -V; /usr/bin/python3.6 -c "import gi" && echo gtk-ok
```

- 没有 `fcitx`（只有 fcitx5 或 ibus）：停止，说明本包只支持 fcitx4。
- `fcitx-remote` 返回 0/1：说明 fcitx 已装但未激活，属于正常，后面会重启。

【第 2 步：下载并解压】
安装目录必须是 `~/workspace/voice-ime`（引擎日志路径在编译时写死在这个位置，装到别处只是丢掉引擎日志，功能不受影响）。

```
mkdir -p ~/workspace && cd ~/workspace
curl -sL -o /tmp/voice-ime.zip 'https://ida.zte.com.cn/zte-paas-lcap-demobff/zte-paas-lcap-demoapi/v1/app/file/uncheck/encryptDownload?fileKey=9e49f03d-8eb8-446c-ac02-bd042f3cc8fd&fileName=voice-ime-2026.10.28.2.zip&encryptEnable=false&securityLevel=null'
md5sum /tmp/voice-ime.zip
```

MD5 必须是 `7e754ae4b7ff2e3eab1440ed773fa506`，不一致就重新下载。

```
unzip -q -o /tmp/voice-ime.zip -d ~/workspace
cd ~/workspace/voice-ime && chmod +x install.sh service.sh *.py
ls
```

期望看到 `install.sh`、`engine/fcitx-voiceime.so`、7 个 `.py`、`config.example.json`。

【第 3 步：依赖检查】
```
cd ~/workspace/voice-ime && ./install.sh --check
```
输出会列出缺的东西。内网 yum 必须显式带 no_proxy，否则走代理报 302：

```
sudo -n env no_proxy=nsdlmirrors.zte.com.cn,.zte.com.cn yum install -y <缺的包>
```

- core 需要 python3.10 及以上，且能 `import websockets`；找不到时用 `~/.local/bin/python3`（可用 `pip install websockets`）。
- 录音浮窗与草稿窗需要 `/usr/bin/python3.6` + GTK3（`python3-gobject`）。缺了不影响语音输入，install.sh 会自动少装两个服务。
- 后面要 `sudo install`，需要 sudo 免密；`sudo -n true` 失败就先让用户执行一次 `sudo -v`。

【第 4 步：填密钥（需要问用户要）】
向用户索要火山引擎语音识别 API Key：控制台「语音技术 → 语音识别 → API Key 管理」，形如一串 UUID。

```
cd ~/workspace/voice-ime
[ -f config.json ] || cp config.example.json config.json
python3 - <<'PY'
import json, pathlib
p = pathlib.Path("config.json")
d = json.loads(p.read_text(encoding="utf-8"))
d["doubao"]["api_key"] = "把用户的 KEY 填在这里"
p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
print("已写入，长度", len(d["doubao"]["api_key"]))
PY
```

不要回显完整密钥，只回报「已写入，长度 N」。LLM 润色默认关闭，不需要配。

【第 5 步：安装并启动】
```
cd ~/workspace/voice-ime && ./install.sh
```

它会：装引擎到 `/usr/lib64/fcitx/`、装 addon 配置与图标、生成 4 个 systemd 用户单元（core / web，有 GTK 时加 osd / draft）、从 fcitx 输入法列表里清掉历史遗留的 voiceime 条目、重启 fcitx、启动服务并自检。

【第 6 步：验证】
```
fcitx-remote                                   # 期望 2（激活态）
curl -s --max-time 3 http://127.0.0.1:19530/api/state
tail -20 /home/*/workspace/voice-ime/logs/voiceime-service.log
```

- `fcitx-remote` 返回 1：按一次 Ctrl+Space 激活输入法，再验一次。
- 然后请用户在一个能打字的地方长按 Ctrl+Alt 说一句话，松手后文字应进入光标处。这一步只能由用户实做，你负责看日志。

【排错】
| 现象 | 处理 |
|---|---|
| 按 Ctrl+Alt 无反应 | 先确认 `fcitx-remote` 是 2；再看 `logs/voiceime-engine.log` 有没有插件加载与按键记录 |
| 识别为空 | `parec --list-device short` 找真实设备名，写进 `config.json` 的 `audio_device`；再查 API Key 与额度 |
| 服务起不来 | `systemctl --user status voiceime-core`，再看 `logs/voiceime-service.log` 末尾；`python3 -m json.tool config.json` 验格式 |
| 拼音打不出来 | `~/.config/fcitx/profile` 的 `EnabledIMList` 里不能有 `voiceime:`（install.sh 会自动清理，被手工加回则再删一次） |
| 想改快捷键 | 打开 http://127.0.0.1:19530 → 快捷键卡片 → 保存并重启 |

【第 7 步：回报】
给出：安装目录、`systemctl --user is-active voiceime-core voiceime-web`、配置台地址、密钥是否已写、用户实测一句话是否成功上屏、还没解决的问题。

---

安装完成后，用户的日常是：拼音照常打字；要说话就长按 Ctrl+Alt，松开即上屏。
