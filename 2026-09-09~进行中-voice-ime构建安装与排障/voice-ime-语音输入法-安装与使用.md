---
title: voice-ime 语音输入法：安装与使用
date: 2026-10-29
tags:
  - voice-ime
  - fcitx
  - 语音输入
  - 安装指南
  - 域/开发经验积累
domain: 开发经验积累
---

# voice-ime 语音输入法：安装与使用

## 为什么做这个

我日常用的是[豆包输入法](https://shurufa.doubao.com/pc)。Windows、安卓、macOS 都有版本，唯独没有 Linux——如果你正好在这三个系统上，直接去下载用就好，不必折腾这个项目。它的语音输入是我用过的里面最能打的，比微信输入法还要再准一截；比起传统搜狗，它没有广告和推荐弹窗，界面干净。

麻烦的是手上这些 Linux 云桌面：豆包输入法装不上，也没有等价的替代品，而等厂商支持不知道要等到什么时候。那就自己把缺的这一截补上。

而它底下的语音能力来自火山引擎（豆包 SeedASR 流式语音识别），这套 API 对外是开放的。所以缺的其实不是识别引擎，只是把「按住说话」接到 Linux 桌面输入框上的这一段——这个项目做的正是这一段。

## 这是个什么

Linux 桌面上按住 Ctrl+Alt 说话，松开即出字，文字直接落在光标处。不占输入法位——原来的搜狗照常可用，说完一句接着用拼音改字，都在同一个输入框里完成。

下面这段是实录：先填热词、看输入历史，再到 iCenter 里聊天说话。

!video[实录 · 填热词、看历史，再到 iCenter 里聊天说 Coclaw](https://ida.zte.com.cn/zte-paas-lcap-demobff/zte-paas-lcap-demoapi/v1/app/file/uncheck/encryptDownload?fileKey=59cf7995-e903-4cce-b732-acc33e7d11b6&fileName=%E8%AF%AD%E9%9F%B3%E8%BE%93%E5%85%A5%E6%B3%95showcase-final.mp4&encryptEnable=false&securityLevel=null)

## 安装

把下面这段引导词整段交给 agent，它负责下载、检查依赖、安装、写密钥、验证。全新安装和升级共用同一段，它会自己判断该走哪条路。

只需要准备一样东西：火山引擎的语音识别 API Key，申请方法见下一节，新账号的免费额度足够日常使用。

```text
【任务】
在这台 Linux 机器上安装或更新 voice-ime 语音输入法：fcitx4 插件 + 豆包流式语音识别，说话即出字，不占用输入法位（拼音照常可用）。这一段步骤全新安装和升级通用——第 2 步先判断是哪种，其余步骤一样跑。安装包里的引擎已经编译好，不需要 gcc、make 或 fcitx-devel。请一步一步执行，每步做完报告结果；某一步失败就停下来说明现象，不要跳过、不要自己换方案。

【第 1 步：确认环境】
这是 Linux + fcitx4 桌面（X11 或 xrdp）。
    echo $XDG_SESSION_TYPE; command -v fcitx && fcitx-remote; python3 -V; /usr/bin/python3.6 -c "import gi" && echo gtk-ok
- 没有 fcitx（只有 fcitx5 或 ibus）：停止，说明本包只支持 fcitx4。
- fcitx-remote 返回 0/1：说明 fcitx 已装但未激活，属于正常，后面会重启。

【第 2 步：判断全新安装还是更新】
    cat ~/workspace/voice-ime/VERSION 2>/dev/null \
      || { [ -d ~/workspace/voice-ime ] && echo "旧版(无 VERSION 文件)" || echo "全新安装"; }
- 输出「全新安装」→ 第 5 步必须问用户要密钥。
- 输出「旧版(无 VERSION 文件)」或旧版本号（如 2026.10.29.11）→ 更新：第 5 步跳过，完成后告诉用户从哪个版本升到 2026.10.29.12。

【第 3 步：下载、校验、覆盖代码】
安装目录必须是 ~/workspace/voice-ime（引擎日志路径在编译时写死在这个位置，装到别处只是丢掉引擎日志，功能不受影响）。
    mkdir -p ~/workspace && cd ~/workspace
    curl -sL -o /tmp/voice-ime.zip 'https://ida.zte.com.cn/zte-paas-lcap-demobff/zte-paas-lcap-demoapi/v1/app/file/uncheck/encryptDownload?fileKey=383a5bdb-15ff-4fc9-ae78-e10dfcb31c99&fileName=voice-ime-2026.10.29.12.zip&encryptEnable=false&securityLevel=null'
    md5sum /tmp/voice-ime.zip
MD5 必须是 c0ad4685ef41350f1c79a131099ff052，不一致就重新下载。
    [ -f ~/workspace/voice-ime/config.json ] && cp -a ~/workspace/voice-ime/config.json ~/workspace/config.json.bak.$(date +%Y%m%d%H%M)
    unzip -q -o /tmp/voice-ime.zip -d ~/workspace
    cd ~/workspace/voice-ime && chmod +x install.sh service.sh *.py && cat VERSION
安装包里不含 config.json 和 logs/，覆盖不会动到凭证和历史日志；上一行的备份只是兜底。

【第 4 步：依赖检查】
    cd ~/workspace/voice-ime && ./install.sh --check
只体检、不装任何东西，缺什么会列出来。内网 yum 必须显式带 no_proxy，否则走代理报 302：
    sudo -n env no_proxy=nsdlmirrors.zte.com.cn,.zte.com.cn yum install -y <缺的包>
- core 需要 python3.10 及以上，且能 import websockets；找不到时用 ~/.local/bin/python3（可用 pip install websockets）。
- 录音浮窗与草稿窗需要 /usr/bin/python3.6 + GTK3（python3-gobject）。缺了不影响语音输入，install.sh 会自动少装两个服务。
- 后面要 sudo install，需要 sudo 免密；sudo -n true 失败就先让用户执行一次 sudo -v。

【第 5 步：密钥（仅全新安装或 key 为空时才做）】
先看现状：
    cd ~/workspace/voice-ime
    if [ -f config.json ]; then
        python3 -c "import json;print('key 长度', len(json.load(open('config.json'))['doubao'].get('api_key','')))"
    else
        cp config.example.json config.json && echo "新配置已生成，需要填 KEY"
    fi
- 已有 config.json 且 key 长度大于 0：跳过本步，不要问用户要密钥。
- 没有 config.json 或 key 为空：向用户索要火山引擎语音识别 API Key（豆包语音控制台 → API Key 管理，形如一串 UUID），再执行：
    python3 - <<'PY'
    import json, pathlib
    p = pathlib.Path("config.json")
    d = json.loads(p.read_text(encoding="utf-8"))
    d["doubao"]["api_key"] = "把用户的 KEY 填在这里"
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已写入，长度", len(d["doubao"]["api_key"]))
    PY
不要回显完整密钥，只回报「已写入，长度 N」。LLM 润色默认关闭，不需要配。
- 更新时补字段：对照 config.json 与 config.example.json，把新版多出来的顶层键补进去，已有值一律保留。

【第 6 步：安装/升级并启动】
    cd ~/workspace/voice-ime && VOICEIME_YUM_NO_PROXY="nsdlmirrors.zte.com.cn,.zte.com.cn" ./install.sh
`VOICEIME_YUM_NO_PROXY` 是给 yum 镜像直连用的：走代理访问内网镜像会报 302。镜像不在内网或本机不走代理时可以省略这个变量。重复执行是安全的：它会覆盖引擎二进制与 addon 配置、重写 systemd 用户单元、清理 fcitx 输入法列表里历史遗留的 voiceime 条目、重启 fcitx 和全部服务，不会动 config.json。

【第 7 步：验证】
    cat ~/workspace/voice-ime/VERSION
    systemctl --user is-active voiceime-core voiceime-web
    systemctl --user show voiceime-core -p ActiveEnterTimestamp
    fcitx-remote
    curl -s --max-time 3 http://127.0.0.1:19530/api/state
    tail -20 ~/workspace/voice-ime/logs/voiceime-service.log
- 更新场景：ActiveEnterTimestamp 应是刚才的时间（服务确实重启过），日志里不应出现 Traceback。
- fcitx-remote 返回 1：按一次 Ctrl+Space 激活输入法，再验一次。
- 然后请用户在一个能打字的地方长按 Ctrl+Alt 说一句话，松手后文字应进入光标处。这一步只能由用户实做，你负责看日志。

【第 8 步：回报】
- 全新安装报：安装目录、VERSION、两个服务是否 active、配置台地址、密钥写入长度、用户实测是否成功上屏。
- 更新报：从哪个版本升到 2026.10.29.12、服务是否全部 active 且刚重启、有没有残留报错、用户实测是否成功上屏。
```

## 火山引擎 API Key 申请

<details>
<summary>展开：三步拿到 Key（新账号 20 小时免费额度）</summary>

API Key 在**豆包语音**控制台申请，不在方舟控制台。

1. **进入控制台**：打开[语音技术控制台](https://console.volcengine.com/speech/new/overview?ResourceID=volc.service_type.10074&projectName=default)，登录并完成实名认证。
2. **开通服务**：左侧「开通管理」→「流式语音识别 2.0」→ 点「开通」。开通后卡片上直接显示剩余时长。
3. **创建 API Key**：左侧「API Key」→「创建 API Key」，名字随意（比如 voice-ime）；建好后点行尾的眼睛图标显示完整密钥，复制那一串 UUID。

![[火山语音控制台-开通服务.png|720]]

![[火山语音控制台-APIKey创建.png|720]]

</details>

## 使用

| 操作 | 行为 |
|---|---|
| 按住 Ctrl+Alt 说话 | 出现声波浮窗，松开即停并上屏 |
| 按住 Ctrl+Alt+Space | 触发后可松手继续说，按 Esc 结束并整段上屏 |
| 录音中单按 Ctrl | 切到草稿窗，内容不进输入框 |
| 停顿约半秒 | 当前句定稿，后面的算新一句 |

两组组合都能在配置台改（点一下输入框直接按想要的键）。默认即停模式，Ctrl+Alt+Tab 切换模式。

浮窗只回答两个问题：有没有在录、润色到哪一步。

| 外观 | 含义 |
|---|---|
| 青蓝声波 | 持续模式：松手后继续录，按 Esc 结束 |
| 橙色声波 | 即停模式（默认）：松开立即上屏，一句话一句 |
| 声波加黄描边 | 润色中，通常 1 到 2 秒 |
| 只有一条细线 | 麦克风没有输入，检查音频设备或远程桌面麦克风重定向 |
| 红边卡片红字 | 语音密钥无效，去配置台点「校验 Key」 |

## 其余功能

- **自动润色**（可选）：接一个 OpenAI 兼容的大模型，把口头语、语病、自我纠正整理成书面语再上屏；失败或超过 8 秒就提交识别原文。默认关闭。
- **热词**：填一份专有名词表，识别时纠音、润色时纠写法，说 `Coclaw` 不再出 `call log`。
- **草稿窗**：录一整段只进窗口不打扰光标，可改错字、可让模型优化，再复制出去。
- **配置台**：浏览器打开 `http://127.0.0.1:19530`，参数、日志、输入历史都在这里。改 API Key 要先点「校验 Key」，通过才能保存。
