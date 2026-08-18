.DEFAULT_GOAL := all

.PHONY: all clean check test visual-test debug asan tsan

all:
	$(MAKE) -C Native all

clean:
	$(MAKE) -C Native clean

check:
	$(MAKE) -C Native check

test:
	$(MAKE) -C Native test

visual-test:
	$(MAKE) -C Native visual-test

debug:
	$(MAKE) -C Native debug

asan:
	$(MAKE) -C Native asan

tsan:
	$(MAKE) -C Native tsan
