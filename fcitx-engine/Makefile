# fcitx-voiceime 引擎构建
FCITX_CFLAGS = -I/usr/include/fcitx -I/usr/include/fcitx-utils -I/usr/include/fcitx-config
GLIB_CFLAGS := $(shell pkg-config --cflags glib-2.0)
GLIB_LIBS := $(shell pkg-config --libs glib-2.0)

# 引擎调试日志写到应用目录 logs/（voice-ime/logs/voiceime-engine.log），编译时注入
CFLAGS = $(FCITX_CFLAGS) $(GLIB_CFLAGS) -O2 -fPIC -Wall -Wextra \
         -DENGINE_LOG_PATH=\"$(abspath $(CURDIR)/..)/logs/voiceime-engine.log\"
LDFLAGS = -shared -lpthread $(GLIB_LIBS) -lfcitx-core -lX11

all: fcitx-voiceime.so

fcitx-voiceime.so: voiceime.c
	gcc $(CFLAGS) -o $@ voiceime.c $(LDFLAGS)

install: fcitx-voiceime.so
	install -m 755 fcitx-voiceime.so /usr/lib64/fcitx/
	install -m 644 fcitx-voiceime.conf /usr/share/fcitx/addon/

uninstall:
	rm -f /usr/lib64/fcitx/fcitx-voiceime.so
	rm -f /usr/share/fcitx/addon/fcitx-voiceime.conf

clean:
	rm -f fcitx-voiceime.so

.PHONY: all install uninstall clean
