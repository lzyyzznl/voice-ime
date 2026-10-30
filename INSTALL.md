# voice-ime 安装与使用指南（面向 AI 代理）

> 供 **AI 代理**（或其他执行者）按步骤完成安装、配置、验证与使用。
> 所有命令可直接复制执行；每步附验证方法，异常时参考「故障排查」。

## 1. 这是什么

Linux 桌面（X11 + fcitx 4）的语音输入插件：**按住 Ctrl+Alt 说话 → 松开即停上屏（对讲机）；豆包流式 ASR 识别 → 可选 LLM 润色 → 文本提交到当前输入光标**（fcitx 4 Module addon，注册全局按键过滤器，**不占输入法位**；不碰剪贴板）。拼音输入法（搜狗等）可常驻激活，打字与语音并存、无需来回切换。包含 4 个 systemd 用户服务：core（主服务）、osd（录音浮窗）、draft（草稿窗）、web（配置台）。

## 2. 前置条件

- Linux + X11 + fcitx 4.x（不支持 Wayland；需 `fcitx-remote` 可用）
- 麦克风（PulseAudio，`parec` 可录音）
- 两个 Python 运行时：
  - **Python 3.11+**（core/web/ASR/润色）：`~/.local/bin/python3`（需 `websockets` 库）
  - **系统 python3.6**（OSD/draft GTK 窗）：RHEL8 自带，需 `python3-gobject` + `gtk3` + `python36-cairo`
- 可访问豆包 ASR：`wss://openspeech.bytedance.com/...`（能上网即可，必要时走代理）
- 润色 LLM 凭证（可选）：DeepSeek 官方 sk- key 或自建 OpenAI 兼容网关的 key
- sudo 免密或可输密码（仅安装引擎二进制时需要）

## 3. 安装步骤

### 3.1 定位代码目录

```bash
cd ~/workspace/voice-ime
```

### 3.2 环境检测

```bash
./check_env.sh
```

- 逐项 `[✓]`/`[✗]`，退出码 = 缺失项数量；`0` = 就绪。

### 3.3 一键安装

```bash
./install.sh
```

自动完成：yum 装缺失依赖（内置 no_proxy）→ 安装 fcitx 引擎（包内自带预编译 `.so`，没有 gcc/make/fcitx-devel 也能装；源码比 `.so` 新时现场编译）→ 图标 → **安装 4 个 systemd 用户单元** → 启动 → 验证。

只体检不改系统：`./install.sh --check`

> 若 `config.json` 已存在则保留（不覆盖凭证）；否则从 `config.example.json` 生成。

### 3.4 配置凭证（豆包 ASR + 润色 LLM）

```bash
vi config.json     # 或浏览器开 http://127.0.0.1:19530 配置台
```

必填：

```json
{
  "doubao": {
    "api_key": "<豆包火山引擎 Key>",
    "resource_id": "volc.seedasr.sauc.duration"
  },
  "llm": {
    "auto_polish": true,
    "active": "deepseek",
    "models": [
      {"id": "deepseek", "name": "DeepSeek 官方",
       "endpoint": "https://api.deepseek.com/v1/chat/completions",
       "api_key": "<sk-...>", "model": "deepseek-chat", "system_prompt": "见 example"}
    ]
  }
}
```

- 豆包 key 为空 → ASR 握手失败，识别报错。
- `llm` 可整体去掉 = 关闭润色（草稿窗「优化」按钮也随之不可用）。
- 其余可调项见 README「配置」表。

### 3.5 重启 fcitx（引擎 .so 只在启动时加载，**必须**）

```bash
pkill fcitx && sleep 2
export DISPLAY=:0 DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus"
setsid fcitx -d
sleep 6
fcitx-remote      # 输出 0/1/2，非报错即可
```

### 3.6 重启服务使配置生效

```bash
systemctl --user restart voiceime-core
# 或 ./service.sh restart（4 个服务一起）
```

### 3.7 验证安装

```bash
# 1. 4 服务全 active
systemctl --user is-active voiceime-core voiceime-osd voiceime-draft voiceime-web

# 2. 插件已加载（fcitx 启动时的 addon，不是输入法）
grep 'addon 已加载' logs/voiceime-engine.log
# 应无输出（voiceime 已不是输入法，返回空即正常）
fcitx-remote -m voiceime

# 3. 引擎已连接 core（1 条 ESTAB）
ss -x | grep voiceime

# 4. Web 配置台可达
curl -s http://127.0.0.1:19530/api/state | head -c 200

# 5. 端到端（手动）：聚焦输入框 → 按住 Ctrl+Alt 说话 → 松开（对讲机即停上屏）
```

## 4. 使用方法

| 操作 | 行为 |
|---|---|
| 输入法 | 保持拼音输入法（搜狗）即可，插件不占输入法位；**无需切换** |
| **按住说话组合**（默认 Ctrl+Alt）≥0.3s | 开始录音（提示音 + 浮窗声波） |
| **松开说话组合** | 对讲机：停止并上屏 |
| **按住持续对话组合**（默认 Ctrl+Alt+Space） | 持续模式：触发后可松手继续说 |
| 持续模式按 **Escape** | 停止并提交剩余文本 |
| 配置台「快捷键」 | 改 `ptt_keys` / `hold_keys`，保存并重启生效 |
| **Ctrl+Alt+Tab** | 切换默认录音模式（两个组合已各自固定模式，一般用不到） |
| 录音中单按 Ctrl（A）/ 空闲 Ctrl+Alt+Shift（B） | 切换草稿模式（内容进草稿窗） |
| 说话 | 停顿 500ms 自动定稿；`auto_polish` 开则先润色再上屏 |
| 超长说话 | 300s 兜底续录（录音不断、内容零丢失；实测 30s 单会话无截断） |

## 5. 服务运维

```bash
systemctl --user status voiceime-core        # 状态
systemctl --user restart voiceime-core       # 改 config 后重启
journalctl --user -u voiceime-core -f        # 日志
./service.sh restart                         # 全部 4 服务重启
./service.sh status
```

## 6. 故障排查

```bash
tail -f logs/voiceime-service.log   # core：命令/录音/识别/润色
tail -f logs/voiceime-engine.log    # 引擎
tail -f logs/voiceime-osd.log       # 浮窗
```

| 症状 | 排查 |
|---|---|
| 按 Ctrl+Alt 无反应 | ① fcitx 未激活：`fcitx-remote` 返回 2 才生效，`fcitx-remote -o` 激活；② 插件未加载：看 `logs/voiceime-engine.log` 是否有「addon 已加载」；③ core 未运行 |
| 日志无「addon 已加载」 | 重启 fcitx（3.5）；确认 `/usr/lib64/fcitx/fcitx-voiceime.so` 与 `/usr/share/fcitx/addon/fcitx-voiceime.conf`（`Category=Module`）；`logs/` 目录须存在（install.sh 会建） |
| 识别报错/无结果 | 豆包 key 空/错；网络不通；看 core 日志 `ASR` 行 |
| 润色失败频繁 | 看日志「润色失败，提交原文」；key 失效/模型到期/超时 8s；配置台切换模型 |
| 润色不生效 | `llm.auto_polish` 未开；active 模型 key 无效 |
| 说话 60s 处断 | 看 service 日志「会话切换」 |
| 浮窗不显示 | python3.6 缺 GTK 绑定：`/usr/bin/python3.6 -c "import gi"` |
| 录音无输入 | `pactl list sources short` 查源名 → config `audio_device` 填真实源（RDP=`xrdp-source`） |

## 7. 卸载

```bash
cd ~/workspace/voice-ime/fcitx-engine && sudo make uninstall
systemctl --user disable --now voiceime-core voiceime-osd voiceime-draft voiceime-web
rm -f ~/.config/systemd/user/voiceime-*.service && systemctl --user daemon-reload
```

## 8. 给 AI 代理的执行提示

- 每步检查退出码；异常先查第 6 节。
- 3.5 重启 fcitx 是引擎生效前提，最易遗漏。
- 改 `config.json` 的 LLM 段**不用重启**（每次现读）；改 ASR/录音参数需重启 core。
- 端到端验证需真实按键+说话；自动化止于 3.7 前三步。
