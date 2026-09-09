# voice-ime 安装与使用指南（面向 AI 代理）

> 本文档供 **AI 代理**（或其他执行者）按步骤完成安装、配置、验证与使用。
> 所有命令均可直接复制执行；每步附验证方法，执行结果异常时参考「故障排查」。

## 1. 这是什么

ZTE 内网环境的语音输入法：按住 **Ctrl+Alt** 说话 → iCenter 流式语音识别 → 文本直接提交到当前输入光标（fcitx4 输入法引擎，不碰剪贴板）。

架构：`fcitx 引擎（C 插件）↔ Unix socket ↔ Python 服务端 ↔ iCenter WebSocket 流式 ASR`

## 2. 前置条件

- Linux + X11（fcitx4 不支持 Wayland）
- fcitx 4.x 已安装且运行（需 `fcitx-remote` 命令可用）
- 麦克风（PulseAudio，`parec` 可录音）
- 内网可访问 iCenter 服务（`wss://icentermsg.dt.zte.com.cn/...`）
- 具备 ASR 凭证（app_id / source / user_id / secret）
- sudo 免密或可输密码

## 3. 安装步骤

### 3.1 定位代码目录

```bash
cd <voice-ime 代码目录>    # 例如: cd /home/xxx/助理工作区/voice-ime
```

### 3.2 环境检测

```bash
./check_env.sh
```

- 输出逐项 `[✓]`/`[✗]`，**退出码 = 缺失项数量**。
- 退出码 `0` = 环境就绪，跳到 3.4。
- 退出码非 `0` = 有缺失依赖，执行 3.3。
- 录音指示浮窗依赖 `python3-gobject` + `gtk3`（RHEL 8 自带；缺失时 `sudo yum -y install python3-gobject gtk3`，须带 no_proxy）。

### 3.3 一键安装（补齐依赖 + 编译引擎 + 启动服务）

```bash
./install.sh
```

install.sh 自动完成：yum 安装缺失依赖（内置 `no_proxy` 规避内网代理 302）→ 编译并安装 fcitx 引擎 → 生成 config.json 模板 → 启动服务 → 验证。

**若 config.json 已存在会跳过生成**（见 3.4 确认凭证）。

### 3.4 配置 ASR 凭证

```bash
cat config.json
```

必须字段（向 iCenter 申请）：

```json
{
  "app_id": "APP_2026_xxxxxx",
  "source": "你的应用名",
  "user_id": "工号",
  "secret": "HMAC 密钥"
}
```

- `secret` 为空/占位 → ASR 无法鉴权，服务启动失败或识别报错。
- 其余可调项见第 6 节。

### 3.5 重启 fcitx（必须！）

引擎 `.so` 只在 fcitx **启动时**加载，安装/更新引擎后必须重启 fcitx：

```bash
pkill fcitx && sleep 2
export DISPLAY=:0 DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus"
setsid fcitx -d
sleep 6
fcitx-remote      # 输出 0/1/2（fcitx 未运行/未激活/激活），非报错即可
```

注意：DBUS 地址必须是用户 session 的 bus（`/run/user/<uid>/bus`），格式 `unix:path=...`（缺前缀 fcitx 解析失败）。

### 3.6 验证安装

```bash
# 1. 服务在跑
pgrep -f voice_ime_service.py && echo "服务运行中"

# 2. 引擎已注册（切换输入法激活 voiceime）
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus"
fcitx-remote -s voiceime
gdbus call --session --dest org.fcitx.Fcitx --object-path /inputmethod \
  --method org.fcitx.Fcitx.InputMethod.GetCurrentIM   # 应返回 ('voiceime',)

# 3. 引擎已连接服务（1 条 ESTAB）
ss -x | grep voiceime

# 4. 端到端（手动操作）：聚焦输入框 → 按住 Ctrl+Alt ≥0.5s → 说话 → Escape
```

## 4. 使用方法

| 操作 | 行为 |
|---|---|
| 切换到「语音输入」 | fcitx 托盘/切换键（该输入法在安装后出现，名为「语音输入」） |
| **按住 Ctrl+Alt ≥0.5s** | 开始录音（提示音 + **桌面浮窗声波动画**，屏幕中央偏上、随音量跳动）→ 可松手 |
| 说话 | 停顿处句子自动上屏（final 即时提交）；preedit 实时显示当前句 |
| **按 Escape** | 停止录音并提交剩余文本（提示音） |
| 长按 <0.5s 松开 | 忽略（防误触） |
| 说话停顿 | 不中断（说过话后静音上限 30s；从未说话 15s 自动停） |
| 说话超过 60s | 自动切换会话无缝续录（录音不中断，内容零丢失） |

**注意**：单次录音受 iCenter 会话 60s 限制，但系统自动无缝续录，无需手动操作；录音上限由 60s 切换机制持续续录直到 Escape。

## 5. 服务运维

```bash
./service.sh start      # 启动（幂等）
./service.sh restart    # 重启（改 config.json 后必须重启才生效）
./service.sh stop       # 停止
```

改配置生效流程：`编辑 config.json → ./service.sh restart`（引擎无需重启；改引擎代码则需 3.5 重启 fcitx）。

## 6. 配置项（config.json）

| 键 | 默认 | 说明 |
|---|---|---|
| `app_id` / `source` / `user_id` / `secret` | — | ASR 凭证（必填） |
| `env` | `prod` | iCenter 环境：`prod` 生产 / 灰度 `gray`（测试环境不可用，1008） |
| `sample_rate` | 16000 | 采样率（Hz），须与凭证开通一致 |
| `chunk_ms` | 200 | 音频分片（毫秒） |
| `audio_device` | `default` | PulseAudio 源名；若 `-d default` 录不到（默认源损坏），显式填源名（RDP 环境为 `xrdp-source`，`pactl list sources short` 可查） |
| `silence_timeout_sec` | 15 | 从未识别到内容时连续静音自动停止（秒），0 关闭 |
| `silence_after_input_sec` | 30 | 已识别到内容后的静音上限（说话中停顿不误断），0 与前者相同 |
| `silence_rms_threshold` | 300 | 静音判定振幅阈值（PCM 16bit RMS），底噪高环境调大 |
| `osd.width` / `osd.height` | 160 / 58 | 录音指示浮窗宽高（px） |
| `osd.y_ratio` | 0.80 | 浮窗垂直位置（屏幕高度比例，0~1；水平始终居中） |
| `focus_lost_stop` | false | 失去焦点自动停止录音并释放麦克风（true）；false = 切窗口不中断录音 |
| `hold_ms` | 300 | 长按 Ctrl+Alt 触发语音输入的阈值（毫秒），服务启动时下发给引擎 |

## 7. 故障排查

日志位置：

```bash
tail -f logs/voiceime-service.log   # 服务端：命令、录音、识别结果、60s 切换
tail -f logs/voiceime-engine.log    # 引擎：按键事件、收到 preedit/commit/done/started
~/.config/fcitx/log/                # fcitx 自身日志
```

| 症状 | 排查 |
|---|---|
| fcitx 无「语音输入」 | 引擎未加载：确认 `ls /usr/lib64/fcitx/fcitx-voiceime.so` 存在 → 重启 fcitx（3.5） |
| 按 Ctrl+Alt 无反应 | 确认当前 IM 是 voiceime（3.6 第 2 步）；fcitx-remote 状态非 0 |
| 识别报"ASR 失败" | config.json 凭证错误/空 → 检查 3.4；网络不通 iCenter |
| 长按后无浮窗/无识别 | 看 service 日志有无 `收到命令: start` 与 `识别结果`；浮窗需 python3-gobject+gtk3（`/usr/bin/python3.6 -c "import gi"` 验证）；浮窗日志 `logs/voiceime-osd.log` |
| 说长话 60s 处断 | 看 service 日志 `60s 切换` 行；若报错看 `续录失败` |
| 引擎日志为空 | fcitx 未激活 voiceime；或 /tmp 无写权限 |
| yum 安装 302 | 必须带 no_proxy（install.sh 已内置） |
| 应用/会议录不到麦克风 | PulseAudio 默认源损坏（`parec -d xrdp-source` 有数据但 `-d default` 静音）：`pactl list sources short` 查真实源名 → `pactl set-default-source <源名>`；或在应用里手动选该源。service.sh 启动时已自动设 xrdp-source（RDP 环境） |

## 8. 卸载

```bash
cd <voice-ime 代码目录>/fcitx-engine
sudo make uninstall        # 移除引擎 .so 与 addon 配置
./service.sh stop
```

## 9. 给 AI 代理的执行提示

- **每步执行后检查退出码/输出**，异常先查第 7 节，不要跳过。
- **3.5 重启 fcitx 是引擎生效的前提**，最容易遗漏。
- 端到端验证需要真实用户操作（按键+说话）无法完全自动化；可用 `check_env.sh` + 3.6 前三步确认系统侧就绪，交互验证交给用户。
- 改 config.json 后必须 `./service.sh restart`；改引擎 C 代码后必须 `make && sudo make install` + 重启 fcitx。
