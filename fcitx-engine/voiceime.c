/*
 * fcitx-voiceime: 语音输入法引擎（fcitx 4）
 * ============================================
 * 通过 Unix socket 与 Python 语音服务（voice_ime_service.py）通信：
 *   - Ctrl+Alt 按住 ≥300ms 触发录音（防误触）
 *   - 松开 Ctrl/Alt：对讲机 B 模式 → 停止并提交（默认）
 *   - Ctrl+Tab 切换 B 对讲机 / A 持续模式；A 下 Escape 停止提交
 *   - 录音中单独 Ctrl（A）或空闲 Ctrl+Alt+Shift 三键（B）→ 切草稿窗
 *   - ASR 中间结果 → preedit 实时更新（20ms 主循环轮询队列）
 *
 * 不依赖剪贴板：文本经 fcitx 的 XIM 管线直接提交。
 */
#include <fcitx/fcitx.h>
#include <fcitx-utils/utils.h>
#include <fcitx/ime.h>
#include <fcitx/keys.h>
#include <fcitx/ui.h>
#include <fcitx/frontend.h>
#include <fcitx/module.h>
#include <fcitx/instance.h>
#include <glib.h>
#include <pthread.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#include <stdarg.h>

#define SOCKET_PATH   "/tmp/voiceime-service.sock"
#define RECV_LINE_MAX 16384
#define HOLD_MS_DEFAULT 300   /* 长按 Ctrl+Alt 触发阈值默认值；服务端 config 下发覆盖 */

/* PTT 触发键：Ctrl+Alt 组合（仅语音输入法激活时生效） */
static boolean is_ctrl(FcitxKeySym sym)
{
    return sym == FcitxKey_Control_L || sym == FcitxKey_Control_R;
}

static boolean is_alt(FcitxKeySym sym)
{
    return sym == FcitxKey_Alt_L || sym == FcitxKey_Alt_R;
}

static boolean is_shift(FcitxKeySym sym)
{
    return sym == FcitxKey_Shift_L || sym == FcitxKey_Shift_R;
}

typedef struct _VoiceIM {
    FcitxInstance* owner;
    int sock;                       /* 到语音服务的 socket */
    pthread_mutex_t send_lock;
    boolean recording;
    boolean ctrl_down;
    boolean alt_down;
    boolean shift_down;             /* Shift 按下状态 */
    boolean shift_solo;             /* Shift 单独按下（期间无其他键）→ 释放时切模式 */
    boolean ctrl_solo;              /* Ctrl 单独按下（期间无其他键）→ 录音中释放时切草稿 */
    boolean mode_b;                 /* 录音模式：false=持续输入(A) true=对讲机(B，按住说松开停) */
    boolean armed;                  /* Ctrl+Alt 已按下，等待 hold_ms 判定 */
    boolean triggered;              /* 本次按下已触发（防 repeat） */
    boolean esc_pressed;            /* Escape 按下状态（防 repeat） */
    boolean tri_pressed;            /* Ctrl+Alt+Shift 三键齐按过（空闲松手切草稿） */
    int hold_ms;                    /* 长按触发阈值（服务端 config 下发） */
    struct timespec press_time;     /* Ctrl+Alt 同时按下的时刻 */
    volatile gboolean recv_running;
    GThread* recv_thread;
    GAsyncQueue* queue;             /* recv_thread → 主循环 消息队列（线程安全） */
} VoiceIM;

static VoiceIM* g_voiceim = NULL;   /* fcitx 单实例 */

static void dbg_log(const char* fmt, ...);  /* 前向声明 */
static void* recv_loop(void* arg);  /* 前向声明（restart_recv 使用） */
static void refresh_ui(VoiceIM* im);  /* 前向声明 */
static void toggle_rec_mode(VoiceIM* im);  /* 前向声明（DoReleaseInput 使用） */
static void toggle_draft(VoiceIM* im);  /* 前向声明（DoReleaseInput 使用） */
static void stop_and_submit(VoiceIM* im);  /* 前向声明（DoInput 使用） */

/* ─────────────────────────── IPC ─────────────────────────── */

static boolean connect_service(VoiceIM* im)
{
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) {
        dbg_log("connect_service: socket 失败");
        return false;
    }
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, SOCKET_PATH, sizeof(addr.sun_path) - 1);
    if (connect(fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        dbg_log("connect_service: connect 失败 (%s)", strerror(errno));
        close(fd);
        return false;
    }
    im->sock = fd;
    dbg_log("connect_service: 已连接 fd=%d", fd);
    return true;
}

static void restart_recv(VoiceIM* im)
{
    if (im->recv_thread) {
        g_thread_join(im->recv_thread);  /* 旧线程读旧 fd，close 后返回 EOF 退出 */
        im->recv_thread = NULL;
    }
    im->recv_running = TRUE;
    im->recv_thread = g_thread_new("voiceime-ipc", recv_loop, im);
}

static void send_cmd(VoiceIM* im, const char* json)
{
    pthread_mutex_lock(&im->send_lock);
    if (im->sock < 0)
        connect_service(im);   /* 未连接/已断开：先重连（unix socket 快速失败） */
    if (im->sock >= 0) {
        ssize_t n = write(im->sock, json, strlen(json));
        if (n >= 0)
            write(im->sock, "\n", 1);
        if (n < 0) { /* 断开，重连并重启接收线程 */
            close(im->sock);
            im->sock = -1;
            if (connect_service(im)) {
                write(im->sock, json, strlen(json));
                write(im->sock, "\n", 1);
                restart_recv(im);
            }
        }
    }
    pthread_mutex_unlock(&im->send_lock);
}

/* 上报"语音输入法是否为当前激活输入法"→ 服务端转发托盘（图标随输入法切换，
   像搜狗：激活显示麦克风，未激活显示键盘）。Init=激活，OnClose(SwitchIM/
   ChangeByInactivate)=失活。 */
static void send_im_active(VoiceIM* im, int active)
{
    char buf[64];
    snprintf(buf, sizeof(buf), "{\"cmd\":\"im_active\",\"active\":%d}", active);
    send_cmd(im, buf);
    dbg_log("IM 激活状态上报: active=%d", active);
}

/* 提取 "text":"..." 的值并反转义（\" \\ \n \t） */
static char* extract_text(const char* line)
{
    const char* p = strstr(line, "\"text\": \"");
    if (p)
        p += 9;          /* "text": "（含内容开引号） */
    else {
        p = strstr(line, "\"text\":\"");
        if (p)
            p += 8;      /* "text":"（含内容开引号） */
    }
    if (!p)
        return NULL;
    char* out = g_malloc(strlen(p) + 1);
    int o = 0;
    while (*p && *p != '"') {
        if (*p == '\\' && *(p + 1)) {
            p++;
            if (*p == 'n')
                out[o++] = '\n';
            else if (*p == 't')
                out[o++] = '\t';
            else
                out[o++] = *p;
        } else {
            out[o++] = *p;
        }
        p++;
    }
    out[o] = '\0';
    return out;
}

/* ─────────────────── 主循环 UI 回调 ──────────────────────── */

typedef struct {
    int type;       /* 1=preedit 2=commit */
    char* text;
} VoiceMsg;

static void clear_preedit(VoiceIM* im)
{
    FcitxInputState* input = FcitxInstanceGetInputState(im->owner);
    if (!input)
        return;
    FcitxMessagesSetMessageCount(FcitxInputStateGetClientPreedit(input), 0);
}

static void refresh_ui(VoiceIM* im)
{
    FcitxUIUpdateInputWindow(im->owner);
}

/* 主循环线程执行：处理 recv_thread 收到的消息。
   fcitx 4 不驱动 glib 默认 context（g_idle_add 永不执行），且
   FcitxInstanceAddTimeout 必须从主线程调用——因此 recv_thread 只入队，
   主线程用 AddTimeout 每 50ms 轮询队列。 */
static void poll_queue_cb(void* data)
{
    VoiceIM* im = data;
    VoiceMsg* m;

    /* 长按 Ctrl+Alt ≥500ms → 切换录音（主循环线程，可安全调 UI） */
    if (im->armed && !im->triggered) {
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        long ms = (now.tv_sec - im->press_time.tv_sec) * 1000
                + (now.tv_nsec - im->press_time.tv_nsec) / 1000000;
        if (ms >= im->hold_ms) {
            im->armed = false;
            im->triggered = true;   /* 防 repeat：需全部释放后才重置 */
            im->ctrl_solo = false;  /* 触发即清 solo：长按松手（Ctrl 后按时）不误切草稿 */
            im->shift_solo = false; /* 同上：不误切录音模式 */
            if (!im->recording) {
                im->recording = true;
                /* 模式保持用户当前选择（默认对讲机 B；Ctrl+Tab 可切持续 A），
                   不再每次强制持续模式 */
                send_cmd(im, im->mode_b
                              ? "{\"cmd\":\"mode\",\"b\":1}"
                              : "{\"cmd\":\"mode\",\"b\":0}");
                dbg_log("长按触发 start（%s）", im->mode_b ? "对讲机 B" : "持续 A");
                send_cmd(im, "{\"cmd\":\"start\"}");
                /* 录音指示由桌面浮窗（声波动画）呈现，不再显示文字 */
            }
        }
    }

    while ((m = g_async_queue_try_pop(im->queue)) != NULL) {
        dbg_log("poll_queue: type=%d text=%s", m->type, m->text ? m->text : "(null)");
        if (im && im->owner) {
            if (m->type == 1) {   /* preedit */
                FcitxInputState* input = FcitxInstanceGetInputState(im->owner);
                if (input) {
                    FcitxMessages* preedit = FcitxInputStateGetClientPreedit(input);
                    FcitxMessagesSetMessageCount(preedit, 0);
                    if (m->text && m->text[0])
                        FcitxMessagesAddMessageStringsAtLast(preedit, MSG_INPUT, m->text);
                    refresh_ui(im);
                }
            } else if (m->type == 2) {   /* commit */
                FcitxInputContext* ic = FcitxInstanceGetCurrentIC(im->owner);
                dbg_log("poll_queue commit: ic=%p", (void*)ic);
                if (ic && m->text[0])
                    FcitxInstanceCommitString(im->owner, ic, m->text);
                clear_preedit(im);
                refresh_ui(im);
            } else if (m->type == 3) {   /* done：会话结束，复位录音状态 */
                dbg_log("poll_queue done: 复位 recording");
                im->recording = false;
                im->ctrl_down = false;
                im->alt_down = false;
                im->shift_down = false;
                im->shift_solo = false;
                im->ctrl_solo = false;
                im->tri_pressed = false;
                im->armed = false;
                im->triggered = false;
                clear_preedit(im);
                refresh_ui(im);
            } else if (m->type == 4) {   /* started：服务端已开始录音（手动/自动续录） */
                dbg_log("poll_queue started: recording=true");
                im->recording = true;
            }
        }
        g_free(m->text);
        g_free(m);
    }
    if (im && im->owner)
        FcitxInstanceAddTimeout(im->owner, 20, poll_queue_cb, im);   /* 继续轮询 */
}

static void schedule_msg(VoiceIM* im, int type, char* text)
{
    VoiceMsg* m = g_malloc(sizeof(VoiceMsg));
    m->type = type;
    m->text = text;
    g_async_queue_push(im->queue, m);
}

/* ─────────────────── 调试日志 ────────────────────────────── */

static void dbg_log(const char* fmt, ...)
{
#ifndef ENGINE_LOG_PATH
#define ENGINE_LOG_PATH "voiceime-engine.log"   /* Makefile -D 注入应用目录 */
#endif
    FILE* f = fopen(ENGINE_LOG_PATH, "a");
    if (!f)
        return;
    /* 统一格式: YYYY-MM-DD HH:MM:SS.mmm（点后 3 位为毫秒） */
    struct timeval tv;
    gettimeofday(&tv, NULL);
    struct tm tm;
    localtime_r(&tv.tv_sec, &tm);
    char ts[32];
    strftime(ts, sizeof(ts), "%Y-%m-%d %H:%M:%S", &tm);
    va_list ap;
    va_start(ap, fmt);
    fprintf(f, "%s.%03ld ", ts, (long)tv.tv_usec / 1000);
    vfprintf(f, fmt, ap);
    fprintf(f, "\n");
    fclose(f);
    va_end(ap);
}


/* ─────────────────── 接收线程 ────────────────────────────── */

static void* recv_loop(void* arg)
{
    VoiceIM* im = arg;
    int fd = im->sock;   /* 捕获当前连接，重连后旧线程仍读旧 fd（close 后 EOF 退出） */
    char line[RECV_LINE_MAX];
    size_t pos = 0;

    dbg_log("recv_loop 启动 fd=%d", fd);

    while (im->recv_running) {
        char buf[1024];
        ssize_t n = read(fd, buf, sizeof(buf));
        if (n <= 0) {
            if (n < 0 && errno == EINTR)
                continue;
            break;
        }
        for (ssize_t i = 0; i < n; i++) {
            if (buf[i] == '\n' || pos >= RECV_LINE_MAX - 1) {
                line[pos] = '\0';
                pos = 0;
                dbg_log("recv 行: %s", line);   /* 调试期：记录所有行 */
                if (strstr(line, "\"preedit\"")) {
                    char* text = extract_text(line);
                    dbg_log("收到 preedit: %s", text ? text : "(null)");
                    if (text)
                        schedule_msg(im, 1, text);
                } else if (strstr(line, "\"commit\"")) {
                    char* text = extract_text(line);
                    dbg_log("收到 commit: %s", text ? text : "(null)");
                    if (text)
                        schedule_msg(im, 2, text);
                } else if (strstr(line, "\"started\"")) {
                    dbg_log("收到 started: 服务端已开始录音（含自动续录）");
                    schedule_msg(im, 4, NULL);
                } else if (strstr(line, "\"done\"")) {
                    dbg_log("收到 done: %s", line);
                    /* 会话结束（服务端自动停止：60s 上限/静音超时/连接断开）——
                       引擎必须在主循环复位 recording，否则无法重新开始 */
                    schedule_msg(im, 3, NULL);
                } else if (strstr(line, "\"config\"")) {
                    /* 服务端下发配置：hold_ms 长按触发阈值 */
                    const char* p = strstr(line, "\"hold_ms\"");
                    if (p && (p = strchr(p, ':')) != NULL) {
                        int v = atoi(p + 1);
                        if (v >= 100 && v <= 3000) {
                            im->hold_ms = v;
                            dbg_log("收到 config: hold_ms=%d", im->hold_ms);
                        }
                    }
                }
            } else {
                line[pos++] = buf[i];
            }
        }
    }
    return NULL;
}

/* ─────────────────── 引擎接口 ────────────────────────────── */

static INPUT_RETURN_VALUE VoiceIMDoInput(void* arg, FcitxKeySym sym, unsigned int state)
{
    VoiceIM* im = arg;
    FCITX_UNUSED(state);

    dbg_log("DoInput sym=%lu ctrl=%d alt=%d shift=%d recording=%d", (unsigned long)sym,
            im->ctrl_down, im->alt_down, im->shift_down, im->recording);

    /* 非 Shift 键按下 → 取消"Shift 单独按下"标记 */
    if (!is_shift(sym))
        im->shift_solo = false;
    /* 非 Ctrl 键按下 → 取消"Ctrl 单独按下"标记（Ctrl+C 等组合键不切草稿） */
    if (!is_ctrl(sym))
        im->ctrl_solo = false;

    if (is_ctrl(sym)) {
        im->ctrl_down = true;
        im->ctrl_solo = true;        /* 单独按下；若期间按了其他键会被上方取消 */
        /* Ctrl+Alt（无 Shift）→ armed 等待 hold_ms 判定；
           Ctrl+Alt+Shift → 三键切草稿，不触发录音 */
        if (im->alt_down && !im->shift_down && !im->triggered) {
            im->armed = true;
            clock_gettime(CLOCK_MONOTONIC, &im->press_time);
        }
        return IRV_TO_PROCESS;
    }
    if (is_alt(sym)) {
        im->alt_down = true;
        if (im->ctrl_down && !im->shift_down && !im->triggered) {
            im->armed = true;
            clock_gettime(CLOCK_MONOTONIC, &im->press_time);
        }
        return IRV_TO_PROCESS;
    }
    if (is_shift(sym)) {
        im->shift_down = true;
        im->shift_solo = true;
        /* Ctrl+Alt+Shift 三键齐按：空闲时切草稿（松手触发）。取消 armed 防误启录音 */
        if (im->ctrl_down && im->alt_down) {
            im->tri_pressed = true;
            im->armed = false;
            im->triggered = true;    /* 防 repeat：三键松手前不再误触发录音 */
            dbg_log("三键齐按：待松手切草稿");
        }
        return IRV_TO_PROCESS;
    }

    /* Ctrl+Tab（Ctrl 按住、无 Alt/Shift）→ 切换录音模式 A/B */
    if (sym == FcitxKey_Tab && im->ctrl_down && !im->alt_down && !im->recording) {
        toggle_rec_mode(im);
        return IRV_DO_NOTHING;   /* 消费，避免切换输入焦点 */
    }

    if (sym == FcitxKey_Escape) {
        if (im->recording && !im->esc_pressed) {
            im->esc_pressed = true;              /* 防 repeat */
            stop_and_submit(im);
            return IRV_DO_NOTHING;
        }
        if (im->recording)
            return IRV_DO_NOTHING;               /* repeat，忽略 */
        return IRV_TO_PROCESS;                   /* 未录音时透传给应用 */
    }

    if (sym == FcitxKey_at) {
        /* 直接提交半角 @：聊天软件（微信/企业微信等 Electron 应用）的 @ 人员
           选择挂在 IME 提交路径上，仅透传按键（IRV_TO_PROCESS → fcitx 转发
           原始 key 事件）不触发。@ 不在 fcitx 标点表，WidePunc 开关不影响其
           路径，必须 commit 才能弹选人。不清 preedit：录音中的 ASR 中间结果
           不受影响。 */
        FcitxInputContext* ic = FcitxInstanceGetCurrentIC(im->owner);
        if (ic) {
            dbg_log("At 键提交: ic=%p", (void*)ic);
            FcitxInstanceCommitString(im->owner, ic, "@");
        }
        return IRV_DO_NOTHING;
    }

    return IRV_TO_PROCESS;
}

static INPUT_RETURN_VALUE VoiceIMDoReleaseInput(void* arg, FcitxKeySym sym, unsigned int state)
{
    VoiceIM* im = arg;
    FCITX_UNUSED(state);
    dbg_log("DoReleaseInput sym=%lu ctrl=%d alt=%d shift=%d recording=%d", (unsigned long)sym,
            im->ctrl_down, im->alt_down, im->shift_down, im->recording);

    if (is_ctrl(sym)) {
        /* A 持续模式录音中单独按 Ctrl 并释放 → 切草稿（对讲机 B 录音时 Ctrl 是触发
           键且 ctrl_solo 已在触发时清除，不会误切） */
        if (im->ctrl_solo && im->recording && !im->mode_b)
            toggle_draft(im);
        /* 对讲机 B：松开触发键之一即停止提交（按住=说，松开=停） */
        if (im->mode_b && im->triggered && im->recording)
            stop_and_submit(im);
        im->ctrl_down = false;
        im->ctrl_solo = false;
    }
    else if (is_alt(sym)) {
        /* 对讲机 B：松开触发键之一即停止提交 */
        if (im->mode_b && im->triggered && im->recording)
            stop_and_submit(im);
        im->alt_down = false;
    }
    else if (is_shift(sym)) {
        /* Ctrl+Alt+Shift 三键齐按后松开 Shift → 空闲切草稿（对讲机模式入口） */
        if (im->tri_pressed) {
            im->tri_pressed = false;
            dbg_log("三键松手：切换草稿模式");
            toggle_draft(im);
        }
        im->shift_down = false;
        im->shift_solo = false;
    }
    else if (sym == FcitxKey_Escape)
        im->esc_pressed = false;

    /* 两键全部释放：重置长按判定状态。
       未触发（<hold_ms 松开）→ 本次作废，不算误触；已触发 → 允许下次再触发 */
    if (!im->ctrl_down && !im->alt_down) {
        im->armed = false;
        im->triggered = false;
        im->tri_pressed = false;   /* 三键未完成（先松了 Ctrl/Alt）也清，防悬挂 */
    }
    return IRV_DO_NOTHING;
}

/* 模式 B（即停）失焦时停止录音：客户端在焦点变化时会把当前 preedit 提交成
   正式文本（XIM 标准行为），若继续录音，服务端后续 on_final 会把同一内容再
   提交一遍 → 重复输出。用 cancel（丢弃服务端剩余）而非 stop：内容已由客户端
   preedit 提交保住，cancel 防止二次输出。模式 A（持续输入）失焦不停录。 */
static void stop_recording_on_lost_focus(VoiceIM* im)
{
    if (im->recording && im->mode_b) {
        im->recording = false;
        dbg_log("即停模式：失焦停止录音（cancel 防重复）");
        send_cmd(im, "{\"cmd\":\"cancel\"}");
    }
}

/* 单独按 Ctrl+Tab → 切换录音模式（A 持续输入 ⇄ B 对讲机），上报服务端转浮窗显示。
   不用 Shift：fcitx 里 Shift 单按被绑定为切输入法，会切走 voiceime。 */
static void toggle_rec_mode(VoiceIM* im)
{
    im->mode_b = !im->mode_b;
    dbg_log("录音模式切换: %s", im->mode_b ? "对讲机(B)" : "持续(A)");
    send_cmd(im, im->mode_b
                  ? "{\"cmd\":\"mode\",\"b\":1}"
                  : "{\"cmd\":\"mode\",\"b\":0}");
}

/* 切草稿模式：A 持续录音中单独按 Ctrl，或任意时刻 Ctrl+Alt+Shift 三键 */
static void toggle_draft(VoiceIM* im)
{
    dbg_log("切换草稿模式");
    send_cmd(im, "{\"cmd\":\"draft\"}");
}

/* 停止并提交当前录音（对讲机模式松开触发键 / Escape 共用） */
static void stop_and_submit(VoiceIM* im)
{
    if (!im->recording)
        return;
    im->recording = false;
    dbg_log("停止并提交（松开触发键/Escape）");
    send_cmd(im, "{\"cmd\":\"stop\"}");
    clear_preedit(im);
    refresh_ui(im);
}

static void VoiceIMReset(void* arg)
{
    VoiceIM* im = arg;
    stop_recording_on_lost_focus(im);
    im->ctrl_down = false;
    im->alt_down = false;
    im->armed = false;
    im->triggered = false;
    im->esc_pressed = false;
    clear_preedit(im);
    refresh_ui(im);
}

static void VoiceIMOnClose(void* arg, FcitxIMCloseEventType event)
{
    VoiceIM* im = arg;
    if (event == CET_SwitchIM) {
        /* 切换输入法：停止录音并释放麦克风，上报输入法失活 */
        send_im_active(im, 0);
        if (im->recording) {
            im->recording = false;
            send_cmd(im, "{\"cmd\":\"cancel\"}");
        }
        VoiceIMReset(arg);
    } else if (event == CET_ChangeByInactivate) {
        /* fcitx 全局停用输入法（inactive 键/fcitx-remote -c）：图标回到键盘状态 */
        send_im_active(im, 0);
    } else {
        /* CET_LostFocus（失焦）等：停止录音（方案 C：客户端会提交 preedit，
           继续录音将重复输出；cancel 丢弃服务端剩余防重复） */
        stop_recording_on_lost_focus(im);
        clear_preedit(im);
        refresh_ui(im);
    }
}

static boolean VoiceIMInit(void* arg)
{
    VoiceIM* im = arg;
    dbg_log("VoiceIMInit: sock=%d recv_thread=%p", im->sock, (void*)im->recv_thread);
    if (im->sock < 0)
        connect_service(im);
    if (im->sock >= 0 && !im->recv_thread) {
        im->recv_running = TRUE;
        im->recv_thread = g_thread_new("voiceime-ipc", recv_loop, im);
    }
    if (im->owner)
        FcitxInstanceAddTimeout(im->owner, 20, poll_queue_cb, im);   /* 主线程轮询队列 */
    send_im_active(im, 1);   /* IM 被激活（切到语音输入法）→ 托盘显示麦克风 */
    im->mode_b = true;       /* 默认对讲机 B：按住说、松开停（Ctrl+Tab 可切持续 A） */
    send_cmd(im, "{\"cmd\":\"mode\",\"b\":1}");   /* 上报初始模式 → 浮窗显示 */
    dbg_log("VoiceIMInit 完成: sock=%d recv_thread=%p", im->sock, (void*)im->recv_thread);
    return true;
}

/* ─────────────────── 模块入口 ────────────────────────────── */

static void* VoiceIMCreate(FcitxInstance* instance)
{
    VoiceIM* im = fcitx_utils_malloc0(sizeof(VoiceIM));
    im->owner = instance;
    im->sock = -1;
    pthread_mutex_init(&im->send_lock, NULL);
    im->recording = false;
    im->mode_b = true;       /* 默认对讲机 B（按住说松开停） */
    im->hold_ms = HOLD_MS_DEFAULT;
    im->queue = g_async_queue_new();
    g_voiceim = im;

    FcitxIMIFace iface;
    memset(&iface, 0, sizeof(iface));
    iface.Init = VoiceIMInit;
    iface.ResetIM = VoiceIMReset;
    iface.DoInput = VoiceIMDoInput;
    iface.DoReleaseInput = VoiceIMDoReleaseInput;
    iface.OnClose = VoiceIMOnClose;
    FcitxInstanceRegisterIMv2(instance, im, "voiceime", "语音输入", "voiceime",
                              iface, 10, "zh_CN");
    return im;
}

static void VoiceIMDestroy(void* arg)
{
    VoiceIM* im = arg;
    im->recv_running = FALSE;
    if (im->sock >= 0) {
        close(im->sock);
        im->sock = -1;
    }
    if (im->recv_thread) {
        g_thread_join(im->recv_thread);
        im->recv_thread = NULL;
    }
    pthread_mutex_destroy(&im->send_lock);
    g_async_queue_unref(im->queue);
    g_voiceim = NULL;
    free(im);
}

FCITX_DEFINE_PLUGIN(fcitx_voiceime, ime2, FcitxIMClass2) = {
    VoiceIMCreate,
    VoiceIMDestroy,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    NULL
};
