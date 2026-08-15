.DEFAULT_GOAL := all

.PHONY: all clean check test debug asan

all:
	$(MAKE) -C Native all

clean:
	$(MAKE) -C Native clean

check:
	$(MAKE) -C Native check

test:
	$(MAKE) -C Native test

debug:
	$(MAKE) -C Native debug

asan:
	$(MAKE) -C Native asan
