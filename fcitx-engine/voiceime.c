/*
 * fcitx-voiceime: 语音输入插件（fcitx 4，Module addon）
 * ============================================
 * 注册全局 pre-input / pre-release 按键过滤器，因此**不占用输入法位**：
 *   - 拼音输入法（搜狗）可常驻激活，字母照常打中文
 *   - Ctrl+Alt 按住 ≥300ms 触发录音（任意输入法激活时都生效）
 *   - 松开 Ctrl/Alt：对讲机 B 模式 → 停止并提交（默认）
 *   - Ctrl+Alt+Tab 切换 B 对讲机 / A 持续模式；A 下 Escape 停止提交
 *   - 录音中单独 Ctrl（A）或空闲 Ctrl+Alt+Shift 三键（B）→ 切草稿窗
 *   - ASR 中间结果 → preedit 实时更新（20ms 主循环轮询队列）
 *
 * 通过 Unix socket 与 Python 语音服务（voice_ime_service.py）通信。
 * 不依赖剪贴板：文本经 fcitx 的 XIM 管线直接提交。
 *
 * 前提：fcitx 处于激活状态（IS_ACTIVE）。Ctrl+Space 关闭输入法后 fcitx
 * 不派发 filter，语音与拼音会同时失效。
 */
#include <fcitx/fcitx.h>
#include <fcitx/hook.h>
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
#include <X11/Xlib.h>
#include <X11/keysym.h>

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

typedef struct _VoiceIM VoiceIM;   /* 组合判定助手需要，结构体定义在后 */

/* ── 可配置触发组合：字符串形如 "ctrl+alt" / "ctrl+alt+space" ── */
#define MOD_CTRL  1
#define MOD_ALT   2
#define MOD_SHIFT 4

static boolean is_modifier(FcitxKeySym sym)
{
    return is_ctrl(sym) || is_alt(sym) || is_shift(sym);
}

/* 键名 → KeySym。只覆盖手柄常用键：字母数字、space/tab/esc/grave。未知返回 0。 */
static FcitxKeySym key_from_name(const char* s)
{
    if (!s || !s[0] || s[1])
        return s && s[0] == '`' ? FcitxKey_grave : 0;
    if (s[0] >= 'A' && s[0] <= 'Z')
        return (FcitxKeySym)(s[0] - 'A' + 'a');
    if ((s[0] >= 'a' && s[0] <= 'z') || (s[0] >= '0' && s[0] <= '9'))
        return (FcitxKeySym)s[0];
    return 0;
}

/* 解析 "ctrl+alt+space"：修饰键进 mods，唯一的普通键进 key。
   返回 FALSE 表示格式非法，调用方保留原配置。 */
static boolean parse_combo(const char* s, int* mods, FcitxKeySym* key)
{
    char buf[64];
    int m = 0;
    FcitxKeySym k = 0;
    size_t len;

    if (!s || !s[0] || (len = strlen(s)) >= sizeof(buf))
        return false;
    memcpy(buf, s, len + 1);

    char* p = buf;
    while (p && *p) {
        char* sep = strchr(p, '+');
        if (sep)
            *sep = '\0';
        if (!strcmp(p, "ctrl")) {
            m |= MOD_CTRL;
        } else if (!strcmp(p, "alt")) {
            m |= MOD_ALT;
        } else if (!strcmp(p, "shift")) {
            m |= MOD_SHIFT;
        } else if (!strcmp(p, "space")) {
            k = FcitxKey_space;
        } else if (!strcmp(p, "tab")) {
            k = FcitxKey_Tab;
        } else if (!strcmp(p, "esc") || !strcmp(p, "escape")) {
            k = FcitxKey_Escape;
        } else {
            FcitxKeySym sym = key_from_name(p);
            if (!sym)
                return false;
            k = sym;
        }
        p = sep ? sep + 1 : NULL;
    }
    if (!m && !k)
        return false;
    *mods = m;
    *key = k;
    return true;
}


struct _VoiceIM {
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
    /* 可配置触发组合（服务端 config 下发）：mods 位掩码 + 可选普通键 */
    int ptt_mods;                   /* 按住说话（对讲机）组合的修饰键 */
    FcitxKeySym ptt_key;            /* 0 = 纯修饰键组合 */
    int hold_mods;                  /* 按住持续对话（持续模式）组合 */
    FcitxKeySym hold_key;
    FcitxKeySym active_key;         /* 当前按住的普通键，组合判定用 */
    boolean armed_hold;             /* 本次 armed 的目标模式：true=持续 A */
    struct timespec press_time;     /* Ctrl+Alt 同时按下的时刻 */
    volatile gboolean recv_running;
    long long last_reconnect_ms;    /* 上次重连时间（poll_queue_cb 限速用） */
    GThread* recv_thread;
    GAsyncQueue* queue;             /* recv_thread → 主循环 消息队列（线程安全） */
    boolean mods_dirty;             /* 本轮 Ctrl/Alt 按住期间夹了组合外的键 → 本轮禁止触发 */
    FcitxInputContext* last_ic;     /* 上一轮轮询的焦点上下文（检测失焦，替代 IM OnClose） */
    Display* xdpy;                  /* 独立 X 连接：读物理键盘状态（不受 grab 影响，懒打开） */
    long long last_phys_check_ms;   /* 物理键检查限速（录音期每 200ms 一次） */
};
static int im_mods(VoiceIM* im)
{
    int m = 0;
    if (im->ctrl_down)  m |= MOD_CTRL;
    if (im->alt_down)   m |= MOD_ALT;
    if (im->shift_down) m |= MOD_SHIFT;
    return m;
}

/* ── 物理键盘状态（XQueryKeymap）────────────────────────────────
 * 全局快捷键（iCenter 的 Ctrl+Alt+Z 等）会 grab 键盘：X 键事件不再送到
 * fcitx，press/release 双双丢失。于是 filter 看到「Ctrl+Alt 一直按着」，
 * 300ms 后误触发录音，松手也收不到 release，录音永远挂住（只能 Esc）。
 * XQueryKeymap 直接读服务器端键位掩码，不受 grab 影响，用它兜底。 */
static Display* phys_display(VoiceIM* im)
{
    if (!im->xdpy)
        im->xdpy = XOpenDisplay(NULL);
    return im->xdpy;
}

static boolean phys_key_down(Display* dpy, const char* keys, KeySym sym)
{
    KeyCode kc = XKeysymToKeycode(dpy, sym);
    return kc && (keys[kc >> 3] & (1 << (kc & 7)));
}

/* 组合外的键是否按着 → 这串按键是别的程序的快捷键（Ctrl+Alt+X），不是语音组合 */
static boolean phys_extra_keys_down(VoiceIM* im)
{
    Display* dpy = phys_display(im);
    char keys[32];
    KeyCode skip[8];
    int n = 0, i, c;

    if (!dpy)
        return false;
    XQueryKeymap(dpy, keys);
    skip[n++] = XKeysymToKeycode(dpy, XK_Control_L);
    skip[n++] = XKeysymToKeycode(dpy, XK_Control_R);
    skip[n++] = XKeysymToKeycode(dpy, XK_Alt_L);
    skip[n++] = XKeysymToKeycode(dpy, XK_Alt_R);
    skip[n++] = XKeysymToKeycode(dpy, XK_Shift_L);
    skip[n++] = XKeysymToKeycode(dpy, XK_Shift_R);
    /* 组合自身的普通键（如 ctrl+alt+space 的 space）不算「额外」 */
    if (im->hold_key && n < 8)
        skip[n++] = XKeysymToKeycode(dpy, (KeySym)im->hold_key);
    if (im->ptt_key && n < 8)
        skip[n++] = XKeysymToKeycode(dpy, (KeySym)im->ptt_key);

    for (c = 8; c < 256; c++) {
        boolean hit = false;
        if (!(keys[c >> 3] & (1 << (c & 7))))
            continue;
        for (i = 0; i < n; i++)
            if (skip[i] == c)
                hit = true;
        if (!hit)
            return true;
    }
    return false;
}

/* 组合的修饰键是否仍物理按下 */
static boolean phys_mods_held(VoiceIM* im, int mods)
{
    Display* dpy = phys_display(im);
    char keys[32];

    if (!dpy)
        return true;   /* 查不到就当还按着，绝不误停 */
    XQueryKeymap(dpy, keys);
    if ((mods & MOD_CTRL)
        && !phys_key_down(dpy, keys, XK_Control_L)
        && !phys_key_down(dpy, keys, XK_Control_R))
        return false;
    if ((mods & MOD_ALT)
        && !phys_key_down(dpy, keys, XK_Alt_L)
        && !phys_key_down(dpy, keys, XK_Alt_R))
        return false;
    if ((mods & MOD_SHIFT)
        && !phys_key_down(dpy, keys, XK_Shift_L)
        && !phys_key_down(dpy, keys, XK_Shift_R))
        return false;
    return true;
}

/* press 判定（严格）：按下的键必须属于组合。纯修饰组合要求修饰键恰好齐全，
   含普通键的组合允许额外修饰键（避免 Ctrl+Alt+Space 与 Ctrl+Alt+Shift+Space 互斥）。 */
static boolean combo_match(VoiceIM* im, int mods, FcitxKeySym key, FcitxKeySym sym)
{
    int m = im_mods(im);
    if (key == 0)
        return is_modifier(sym) && m == mods;
    return sym == key && (m & mods) == mods;
}

/* release 判定（宽松）：组合是否仍按住。额外修饰键不影响，避免松开无关键误停。 */
static boolean combo_held(VoiceIM* im, int mods, FcitxKeySym key)
{
    if ((im_mods(im) & mods) != mods)
        return false;
    return key == 0 || im->active_key == key;
}

static VoiceIM* g_voiceim = NULL;   /* fcitx 单实例 */

static void dbg_log(const char* fmt, ...);  /* 前向声明 */
static void* recv_loop(void* arg);  /* 前向声明（restart_recv 使用） */
static void refresh_ui(VoiceIM* im);  /* 前向声明 */
static void toggle_rec_mode(VoiceIM* im);  /* 前向声明（DoReleaseInput 使用） */
static void toggle_draft(VoiceIM* im);  /* 前向声明（DoReleaseInput 使用） */
static void stop_and_submit(VoiceIM* im);  /* 前向声明（DoInput 使用） */
static void stop_recording_on_lost_focus(VoiceIM* im);  /* 前向声明（poll_queue_cb 使用） */

/* 组合键按下后进入 armed，等待主循环判定 hold_ms。
   hold（持续对话）优先于 ptt（按住说话）：当前者是后者的扩展组合
   （如 ctrl+alt+space 之于 ctrl+alt）时，先按修饰键只是预兆，
   按下扩展普通键即升级为持续模式。 */
static void arm_review_combo(VoiceIM* im, FcitxKeySym sym)
{
    if (im->triggered || im->mods_dirty)
        return;
    if (combo_match(im, im->hold_mods, im->hold_key, sym)) {
        im->armed = true;
        im->armed_hold = true;
        clock_gettime(CLOCK_MONOTONIC, &im->press_time);
    } else if (combo_match(im, im->ptt_mods, im->ptt_key, sym)) {
        im->armed = true;
        im->armed_hold = false;
        clock_gettime(CLOCK_MONOTONIC, &im->press_time);
    }
}

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
    /* 发送超时 500ms：服务端（client_loop）不读时 write() 最多等 0.5s 返回
       EWOULDBLOCK，绝不无限阻塞 fcitx 主线程（send_cmd 全部在主线程执行） */
    struct timeval snd_tv;
    snd_tv.tv_sec = 0;
    snd_tv.tv_usec = 500000;
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &snd_tv, sizeof(snd_tv));
    dbg_log("connect_service: 已连接 fd=%d", fd);
    return true;
}

/* 安全停掉旧接收线程后再重连。Linux 上 close() 不会唤醒另一线程阻塞中的
   read()，若随后 connect 复用了同一 fd 号，旧线程会读到新连接而永不退出，
   g_thread_join 永久卡死 fcitx 主循环。必须先 shutdown(SHUT_RDWR) 唤醒
   read（read 返回 0/错误）→ join（有界）→ 再 close、再重连。 */
static void stop_recv_then_close(VoiceIM* im, int fd)
{
    im->recv_running = FALSE;
    if (fd >= 0)
        shutdown(fd, SHUT_RDWR);      /* 唤醒旧线程的阻塞 read */
    if (im->recv_thread) {
        g_thread_join(im->recv_thread);   /* 此刻 read 必已返回，有界 */
        im->recv_thread = NULL;
    }
    if (fd >= 0)
        close(fd);
}

/* 断连/重连后复位状态机：断连期间会丢掉服务端的 started / done 消息，
   若不复位，triggered 可能永久卡在 TRUE —— 而 arm_review_combo 第一行就是
   if (triggered) return，之后对任何组合键都没反应（症状："按什么键都呼不
   出来"，只能 fcitx -r 恢复）。 */
static void reset_stream_state(VoiceIM* im)
{
    im->armed = false;
    im->triggered = false;
    im->recording = false;
    im->ctrl_down = false;
    im->alt_down = false;
    im->shift_down = false;
    im->ctrl_solo = false;
    im->shift_solo = false;
    im->tri_pressed = false;
    im->esc_pressed = false;
    im->active_key = 0;
}

static void send_cmd(VoiceIM* im, const char* json)
{
    pthread_mutex_lock(&im->send_lock);
    if (im->sock < 0)
        connect_service(im);   /* 未连接/已断开：先重连（unix socket 快速失败） */
    if (im->sock >= 0) {
        ssize_t n = write(im->sock, json, strlen(json));
        if (n < 0 && errno == EINTR)
            n = write(im->sock, json, strlen(json));
        if (n >= 0) {
            n = write(im->sock, "\n", 1);
            if (n < 0 && errno == EINTR)
                write(im->sock, "\n", 1);
        }
        if (n < 0) { /* 断开或发送超时：安全停旧线程后重连 */
            stop_recv_then_close(im, im->sock);
            im->sock = -1;
            if (connect_service(im)) {   /* 复用 fd 号时旧线程已结束，无竞态 */
                reset_stream_state(im);  /* 断连丢了 done/started，复位防卡死 */
                write(im->sock, json, strlen(json));
                write(im->sock, "\n", 1);
                im->recv_running = TRUE;
                im->recv_thread = g_thread_new("voiceime-ipc", recv_loop, im);
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

    /* 接收线程已退出（core 重启/崩溃/连接被重置）→ 主动重连。
       原先只有 send_cmd 写失败时才重连，而 core 重启后没人发命令 → 引擎
       永久失联：按键照常打 filter 日志，但 start 永远发不出去（表现为
       "按什么键都呼不出来"，只能 fcitx -r）。限速 1 秒一次。 */
    if (!im->recv_running && im->sock >= 0) {
        struct timespec ts;
        clock_gettime(CLOCK_MONOTONIC, &ts);
        long long now_ms = (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
        if (now_ms - im->last_reconnect_ms > 1000) {
            im->last_reconnect_ms = now_ms;
            dbg_log("接收线程已退出，重连服务端");
            stop_recv_then_close(im, im->sock);
            im->sock = -1;
            if (connect_service(im)) {
                reset_stream_state(im);   /* 断连丢了 done/started，复位防卡死 */
                im->recv_running = TRUE;
                im->recv_thread = g_thread_new("voiceime-ipc", recv_loop, im);
            }
        }
    }

    /* 焦点变化检测（替代原 IM OnClose）：IC 变了视为失焦，停录并清 preedit。
       Cancel 语义见 stop_recording_on_lost_focus 注释。 */
    {
        FcitxInputContext* ic = FcitxInstanceGetCurrentIC(im->owner);
        if (ic != im->last_ic) {
            if (im->last_ic) {
                dbg_log("焦点变化: %p → %p", (void*)im->last_ic, (void*)ic);
                stop_recording_on_lost_focus(im);
                clear_preedit(im);
                refresh_ui(im);
            }
            im->last_ic = ic;
        }
    }

    /* 长按 ≥hold_ms → 开始录音（主循环线程，可安全调 UI）。
       模式由命中的组合决定：按住说话 → 对讲机 B；按住持续对话 → 持续 A。 */
    if (im->armed && !im->triggered) {
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        long ms = (now.tv_sec - im->press_time.tv_sec) * 1000
                + (now.tv_nsec - im->press_time.tv_nsec) / 1000000;
        if (ms >= im->hold_ms) {
            /* 防「Ctrl+Alt+X 类全局快捷键」误触发，三个守卫：
               1) 本轮夹过组合外的键（事件路径就能看到，最可靠）；
               2) 组合外还有键按着（iCenter 的 Ctrl+Alt+Z：X 键事件被对方 grab，
                  filter 收不到 Z，只能靠 XQueryKeymap 看到）；
               3) 当前没有输入上下文（弹窗夺走焦点）→ 录了也无处上屏。 */
            boolean extra = phys_extra_keys_down(im);
            if (im->mods_dirty || extra || !FcitxInstanceGetCurrentIC(im->owner)) {
                dbg_log("长按判定撤销（dirty=%d 组合外按键=%d 有输入上下文=%d）",
                        im->mods_dirty, extra, FcitxInstanceGetCurrentIC(im->owner) != NULL);
                im->armed = false;
                im->tri_pressed = false;
            } else {
                im->armed = false;
                im->triggered = true;   /* 防 repeat：需全部释放后才重置 */
                im->ctrl_solo = false;  /* 触发即清 solo：长按松手（Ctrl 后按时）不误切草稿 */
                im->shift_solo = false; /* 同上：不误切录音模式 */
                if (!im->recording) {
                    im->recording = true;
                    im->mode_b = !im->armed_hold;   /* hold 组合 → 持续 A，否则对讲机 B */
                    send_cmd(im, im->mode_b
                                  ? "{\"cmd\":\"mode\",\"b\":1}"
                                  : "{\"cmd\":\"mode\",\"b\":0}");
                    dbg_log("长按触发 start（%s）", im->mode_b ? "对讲机 B" : "持续 A");
                    send_cmd(im, "{\"cmd\":\"start\"}");
                    /* 录音指示由桌面浮窗（声波动画）呈现，不再显示文字 */
                }
            }
        }
    }

    /* 松开兜底：全局快捷键 grab / 焦点切换会让 release 事件丢失，录音会一直挂着
       （表现为「松手不消失，只能 Esc」）。直接读物理键盘状态判定触发键是否已松开。
       仅对讲机 B：A 模式本来就靠 Esc / 再按组合停止。 */
    if (im->recording && im->mode_b && im->triggered) {
        struct timespec now;
        long long now_ms;
        clock_gettime(CLOCK_MONOTONIC, &now);
        now_ms = (long long)now.tv_sec * 1000 + now.tv_nsec / 1000000;
        if (now_ms - im->last_phys_check_ms >= 200) {
            im->last_phys_check_ms = now_ms;
            if (!phys_mods_held(im, im->ptt_mods)) {
                dbg_log("触发键已物理松开（release 丢失），停止并提交");
                stop_and_submit(im);
            }
        }
    }

    /* 「本轮污染」标记的兜底清除：release 被 grab 吃掉时 ctrl_down 会卡住 true，
       仅靠 release filter 复位会让语音永久失效。物理键全松才查（罕见路径，无开销）。 */
    if (im->mods_dirty && !phys_mods_held(im, MOD_CTRL | MOD_ALT | MOD_SHIFT))
        im->mods_dirty = false;

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

/* 从 config 行里取 "name": "value" 的字符串值；找不到或超长返回 NULL。 */
static const char* json_str_field(const char* line, const char* name, char* out, size_t cap)
{
    char pat[48];
    snprintf(pat, sizeof(pat), "\"%s\"", name);
    const char* p = strstr(line, pat);
    if (!p)
        return NULL;
    p = strchr(p + strlen(pat), ':');
    if (!p)
        return NULL;
    p = strchr(p, '"');
    if (!p)
        return NULL;
    const char* q = strchr(p + 1, '"');
    if (!q)
        return NULL;
    size_t n = (size_t)(q - p - 1);
    if (n >= cap)
        return NULL;
    memcpy(out, p + 1, n);
    out[n] = '\0';
    return out;
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
                    /* 服务端下发配置：hold_ms 长按阈值 + 两个触发组合 */
                    const char* p = strstr(line, "\"hold_ms\"");
                    if (p && (p = strchr(p, ':')) != NULL) {
                        int v = atoi(p + 1);
                        if (v >= 100 && v <= 3000) {
                            im->hold_ms = v;
                            dbg_log("收到 config: hold_ms=%d", im->hold_ms);
                        }
                    }
                    char vb1[64], vb2[64];
                    const char* ptt = json_str_field(line, "ptt_keys", vb1, sizeof(vb1));
                    if (ptt) {
                        int m = 0;
                        FcitxKeySym k = 0;
                        if (parse_combo(ptt, &m, &k)) {
                            im->ptt_mods = m;
                            im->ptt_key = k;
                            dbg_log("收到 config: ptt_keys=%s", ptt);
                        } else {
                            dbg_log("config: ptt_keys 非法，保留原值: %s", ptt);
                        }
                    }
                    const char* hold = json_str_field(line, "hold_keys", vb2, sizeof(vb2));
                    if (hold) {
                        int m = 0;
                        FcitxKeySym k = 0;
                        if (parse_combo(hold, &m, &k)) {
                            im->hold_mods = m;
                            im->hold_key = k;
                            dbg_log("收到 config: hold_keys=%s", hold);
                        } else {
                            dbg_log("config: hold_keys 非法，保留原值: %s", hold);
                        }
                    }
                }
            } else {
                line[pos++] = buf[i];
            }
        }
    }
    im->recv_running = FALSE;   /* 线程退出（EOF/错误/被 shutdown 唤醒）时清扫标志 */
    return NULL;
}

/* ─────────────────── 引擎接口 ────────────────────────────── */

/* pre-input 过滤器：任意 IM（搜狗拼音/键盘）激活时都会收到按键。
   返回 TRUE = 消费按键（*retval 生效），FALSE = 继续交给当前 IM 与应用。
   不消费任何普通字符键，因此拼音输入完全不受影响。 */
static boolean VoiceInputFilter(void* arg, FcitxKeySym sym, unsigned int state,
                                INPUT_RETURN_VALUE* retval)
{
    VoiceIM* im = arg;

    /* 用 fcitx 权威 mods 校准自维护标志：state 源自 X server 物理键盘状态，不会随
       XIM 事件丢失。否则若某次 modifier 释放事件丢失（尤其 Shift 被 fcitx 用作切
       输入法时），shift_down 会卡在 true，Ctrl+Alt 触发条件 !shift_down 永远不满足
       → 无法呼出录音。state 权威 + 当前键自身叠加，正常无泄漏时与原逻辑一致。 */
    im->ctrl_down  = !!(state & FcitxKeyState_Ctrl)  || is_ctrl(sym);
    im->alt_down   = !!(state & FcitxKeyState_Alt)   || is_alt(sym);
    im->shift_down = !!(state & FcitxKeyState_Shift) || is_shift(sym);

    dbg_log("filter press sym=%lu ctrl=%d alt=%d shift=%d recording=%d", (unsigned long)sym,
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
        arm_review_combo(im, sym);
        return false;                /* 修饰键透传：应用仍收到 Ctrl 按下 */
    }
    if (is_alt(sym)) {
        im->alt_down = true;
        arm_review_combo(im, sym);
        return false;
    }
    if (is_shift(sym)) {
        im->shift_down = true;
        im->shift_solo = true;
        /* 按住说话组合 + Shift 三键齐按：空闲时切草稿（松手触发）。
           组合本身已含 Shift 时不存在三键语义，直接走正常 arm。 */
        if (im->ptt_key == 0 && !(im->ptt_mods & MOD_SHIFT)
            && im_mods(im) == (im->ptt_mods | MOD_SHIFT)) {
            im->tri_pressed = true;
            im->armed = false;
            im->triggered = true;    /* 防 repeat：三键松手前不再误触发录音 */
            dbg_log("三键齐按：待松手切草稿");
        } else {
            arm_review_combo(im, sym);
        }
        return false;
    }
    /* 普通键：命中组合才记录 active_key（避免无关按键干扰组合判定），
       并在组合已生效时消费该键——否则按住说话时这个键（空格等）的
       自动重复会被打进当前输入框。组合未按满则透传，不影响正常打字。 */
    if (sym == im->ptt_key || sym == im->hold_key)
        im->active_key = sym;
    /* 组合外的普通键：这轮 Ctrl/Alt 按住是别的程序的快捷键（Ctrl+Alt+Z 等），
       本轮不再允许触发语音，直到修饰键全部松开。否则先按 Ctrl+Alt 再敲键，
       长按判定照样在 hold_ms 后开工。 */
    if (sym != im->ptt_key && sym != im->hold_key
        && !im->triggered && (im->ctrl_down || im->alt_down))
        im->mods_dirty = true;
    arm_review_combo(im, sym);
    if (combo_match(im, im->hold_mods, im->hold_key, sym)
        || combo_match(im, im->ptt_mods, im->ptt_key, sym)
        || (im->recording && (sym == im->ptt_key || sym == im->hold_key))) {
        *retval = IRV_DO_NOTHING;
        return true;
    }

    /* Ctrl+Alt+Tab（无 Shift）→ 切换录音模式 A/B。
       不用裸 Ctrl+Tab：过滤器是全局的，裸 Ctrl+Tab 属于浏览器/应用的切标签键。 */
    if (sym == FcitxKey_Tab && im->ctrl_down && im->alt_down && !im->shift_down
        && !im->recording) {
        toggle_rec_mode(im);
        *retval = IRV_DO_NOTHING;
        return true;
    }

    if (sym == FcitxKey_Escape) {
        if (im->recording && !im->esc_pressed) {
            im->esc_pressed = true;              /* 防 repeat */
            stop_and_submit(im);
            *retval = IRV_DO_NOTHING;
            return true;
        }
        if (im->recording) {
            *retval = IRV_DO_NOTHING;            /* repeat，忽略 */
            return true;
        }
        return false;                            /* 未录音时透传给应用/搜索框 */
    }

    /* 其余字符键一律不消费：拼音输入法照常工作。
       （@ 也不再特判：搜狗自己会走 IME 提交路径，微信 @人 正常弹出） */
    return false;
}

/* pre-release 过滤器：与 press 对称，只跑状态机、**不消费任何释放事件**。
   原因：press 阶段修饰键是透传的（否则 Ctrl+Alt+X 之类组合键会被抢），
   释放若消费就会让应用/输入法看到"按下有、释放无"的不配对状态。
   Esc 的 press 已在录音时被消费，release 透传无副作用。 */
static boolean VoiceReleaseFilter(void* arg, FcitxKeySym sym, unsigned int state,
                                  INPUT_RETURN_VALUE* retval)
{
    VoiceIM* im = arg;
    (void)retval;

    /* 与 press 同：用 state 校准自维护标志，防 modifier 释放事件丢失导致状态卡死 */
    im->ctrl_down  = !!(state & FcitxKeyState_Ctrl)  || is_ctrl(sym);
    im->alt_down   = !!(state & FcitxKeyState_Alt)   || is_alt(sym);
    im->shift_down = !!(state & FcitxKeyState_Shift) || is_shift(sym);

    dbg_log("filter release sym=%lu ctrl=%d alt=%d shift=%d recording=%d", (unsigned long)sym,
            im->ctrl_down, im->alt_down, im->shift_down, im->recording);

    /* 松开普通键：清 active_key，供组合是否仍按住的判定 */
    if (!is_modifier(sym) && im->active_key == sym)
        im->active_key = 0;

    if (is_ctrl(sym)) {
        /* A 持续模式录音中单独按 Ctrl 并释放 → 切草稿（对讲机 B 录音时 Ctrl 是触发
           键且 ctrl_solo 已在触发时清除，不会误切） */
        if (im->ctrl_solo && im->recording && !im->mode_b)
            toggle_draft(im);
        im->ctrl_down = false;
        im->ctrl_solo = false;
    }
    else if (is_alt(sym)) {
        im->alt_down = false;
    }
    else if (is_shift(sym)) {
        /* 按住说话组合 + Shift 三键齐按后松开 Shift → 空闲切草稿 */
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

    /* 对讲机 B：按住说话组合被松开即停止提交（按住=说，松开=停） */
    if (im->mode_b && im->triggered && im->recording
        && !combo_held(im, im->ptt_mods, im->ptt_key))
        stop_and_submit(im);

    /* 两个组合都已松开：重置长按判定状态。
       未触发（<hold_ms 松开）→ 本次作废，不算误触；已触发 → 允许下次再触发 */
    if (!combo_held(im, im->ptt_mods, im->ptt_key)
        && !combo_held(im, im->hold_mods, im->hold_key)) {
        im->armed = false;
        im->triggered = false;
        im->tri_pressed = false;   /* 组合未完成（先松了修饰键）也清，防悬挂 */
        if (!im->ctrl_down && !im->alt_down && !im->shift_down)
            im->mods_dirty = false;   /* 修饰键全松：下一轮重新判定 */
    }
    return false;
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

/* Ctrl+Alt+Tab → 切换录音模式（A 持续输入 ⇄ B 对讲机），上报服务端转浮窗显示。
   不用裸 Ctrl+Tab：全局过滤器会抢走浏览器/应用的切标签键。 */
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
    /* 默认快捷键：按住 Ctrl+Alt 说话（对讲机）；按住 Ctrl+Alt+Space 持续对话。
       服务端 config 下发 ptt_keys / hold_keys 时覆盖。 */
    im->ptt_mods = MOD_CTRL | MOD_ALT;
    im->ptt_key = 0;
    im->hold_mods = MOD_CTRL | MOD_ALT;
    im->hold_key = FcitxKey_space;
    im->queue = g_async_queue_new();
    g_voiceim = im;

    /* 注册全局按键过滤器：不占用输入法位，拼音输入法可常驻激活 */
    FcitxKeyFilterHook pressHook = { VoiceInputFilter, im };
    FcitxKeyFilterHook releaseHook = { VoiceReleaseFilter, im };
    FcitxInstanceRegisterPreInputFilter(instance, pressHook);
    FcitxInstanceRegisterPreReleaseInputFilter(instance, releaseHook);

    if (im->sock < 0)
        connect_service(im);
    if (im->sock >= 0) {
        im->recv_running = TRUE;
        im->recv_thread = g_thread_new("voiceime-ipc", recv_loop, im);
    }
    FcitxInstanceAddTimeout(im->owner, 20, poll_queue_cb, im);   /* 主线程轮询队列 */
    send_im_active(im, 1);   /* 插件常驻 → 托盘显示麦克风 */
    send_cmd(im, "{\"cmd\":\"mode\",\"b\":1}");   /* 上报初始模式 → 浮窗显示 */
    dbg_log("voiceime addon 已加载: sock=%d recv_thread=%p", im->sock, (void*)im->recv_thread);
    return im;
}

static void VoiceIMDestroy(void* arg)
{
    VoiceIM* im = arg;
    send_im_active(im, 0);
    im->recv_running = FALSE;
    if (im->sock >= 0) {
        shutdown(im->sock, SHUT_RDWR);   /* 先唤醒阻塞 read 再 join，防销毁时卡死 */
        close(im->sock);
        im->sock = -1;
    }
    if (im->recv_thread) {
        g_thread_join(im->recv_thread);
        im->recv_thread = NULL;
    }
    pthread_mutex_destroy(&im->send_lock);
    g_async_queue_unref(im->queue);
    if (im->xdpy)
        XCloseDisplay(im->xdpy);
    g_voiceim = NULL;
    free(im);
}

FCITX_DEFINE_PLUGIN(fcitx_voiceime, module, FcitxModule) = {
    VoiceIMCreate,
    NULL,   /* SetFD */
    NULL,   /* ProcessEvent */
    VoiceIMDestroy,
    NULL    /* ReloadConfig */
};
