# Native messaging host for ChessListener.
# main.c is the host (browser length-prefix framing); uci.c is the engine glue.
# analysis.c runs the engines on their own thread, so -pthread is required.

CC      ?= cc
CFLAGS  ?= -std=c11 -O2 -Wall -Wextra -Wpedantic -pthread
LDFLAGS ?= -pthread
TARGET   = chess-listener-host
OBJS     = main.o uci.o overlay.o analysis.o

$(TARGET): $(OBJS)
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS)

main.o:      main.c     analysis.h
analysis.o:  analysis.c analysis.h uci.h overlay.h
uci.o:       uci.c      uci.h
overlay.o:   overlay.c  overlay.h uci.h

clean:
	rm -f $(TARGET) $(OBJS)

.PHONY: clean
