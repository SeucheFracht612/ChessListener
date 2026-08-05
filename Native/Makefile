# Native messaging host for ChessListener.
# main.c is YOUR host (Chrome length-prefix framing); uci.c is the engine glue.

CC      ?= cc
CFLAGS  ?= -std=c11 -O2 -Wall -Wextra -Wpedantic
TARGET   = chess-listener-host
OBJS     = main.o uci.o overlay.o analysis.o

$(TARGET): $(OBJS)
	$(CC) $(CFLAGS) -o $@ $^

main.o:      main.c     analysis.h
analysis.o:  analysis.c analysis.h uci.h overlay.h
uci.o:      uci.c     uci.h
overlay.o:  overlay.c overlay.h uci.h

clean:
	rm -f $(TARGET) $(OBJS)

.PHONY: clean
