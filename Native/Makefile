# Native messaging host for ChessListener.

CC        ?= cc
PYTHON    ?= python3
NODE      ?= node
TARGET    ?= chess-listener-host
BUILD_DIR ?= .build/release
VISUAL_OUTPUT ?= $(abspath .build/visual-ui)
VISUAL_STRICT ?= 1

CPPFLAGS ?=
CFLAGS   ?= -std=c11 -O2 -Wall -Wextra -Wpedantic -pthread
LDFLAGS  ?=
LDLIBS   ?= -pthread

SOURCES := main.c uci.c overlay.c analysis.c
OBJECTS := $(addprefix $(BUILD_DIR)/,$(SOURCES:.c=.o))
DEPS    := $(OBJECTS:.o=.d)

.DEFAULT_GOAL := all

.PHONY: all clean check check-python check-js check-shell test test-js visual-test debug asan tsan

all: $(TARGET)

$(TARGET): $(OBJECTS)
	$(CC) $(LDFLAGS) -o $@ $^ $(LDLIBS)

$(BUILD_DIR)/%.o: %.c | $(BUILD_DIR)
	$(CC) $(CPPFLAGS) $(CFLAGS) -MMD -MP -c -o $@ $<

$(BUILD_DIR):
	mkdir -p $@

# Parse source files without importing PyQt or writing __pycache__ directories.
check-python:
	$(PYTHON) -c 'import ast, pathlib, sys; files = [pathlib.Path(name) for name in sys.argv[1:]]; [ast.parse(path.read_text(encoding="utf-8"), filename=str(path)) for path in files]; print(f"Python syntax: {len(files)} files OK")' overlay.py san.py explanations.py review.py study_store.py study.py pgn_import.py Tests/*.py

check-js:
	$(NODE) --check ../Extension/background.js
	$(NODE) --check ../Extension/content.js
	@if [ -f ../Extension/popup.js ]; then \
		$(NODE) --check ../Extension/popup.js; \
	fi
	@for file in ../Extension/Tests/*.js ../Extension/Tests/*.cjs; do \
		[ -e "$$file" ] || continue; \
		$(NODE) --check "$$file"; \
	done

check-shell:
	bash -n install.sh uninstall.sh update.sh

check: check-python check-js check-shell

test-js:
	@found=0; \
	for file in ../Extension/Tests/*.js; do \
		[ -e "$$file" ] || continue; \
		found=1; \
		echo "Running $$file"; \
		$(NODE) "$$file" || exit $$?; \
	done; \
	if [ "$$found" -eq 0 ]; then echo "No extension tests found"; fi

# Optional on purpose: the normal suite remains usable on minimal systems
# without PyQt6, while design/release work gets the complete strict matrix.
visual-test:
	@set -e; if $(PYTHON) -c 'from PyQt6.QtWidgets import QApplication' >/dev/null 2>&1; then \
		mkdir -p "$(VISUAL_OUTPUT)"; \
		CHESSLISTENER_VISUAL_OUTPUT="$(VISUAL_OUTPUT)" \
			$(NODE) ../Extension/Tests/capture_popup_visual.cjs; \
		QT_QPA_PLATFORM=offscreen \
		CHESSLISTENER_VISUAL_OUTPUT="$(VISUAL_OUTPUT)" \
		CHESSLISTENER_VISUAL_STRICT="$(VISUAL_STRICT)" \
			$(PYTHON) Tests/test_visual_ui.py; \
		if [ -f "$(VISUAL_OUTPUT)/index.html" ]; then \
			echo "Visual contact sheet: $(VISUAL_OUTPUT)/index.html"; \
		else \
			echo "Visual matrix skipped (Qt runtime unavailable; no contact sheet produced)."; \
		fi; \
	else \
		echo "Visual matrix skipped (PyQt6 QtWidgets runtime is not available)."; \
	fi

# -B is deliberate: tests must exercise objects compiled from the current
# checkout, never old or accidentally committed build products.
test:
	$(MAKE) --no-print-directory -B $(TARGET)
	$(MAKE) --no-print-directory check
	$(MAKE) --no-print-directory test-js
	$(PYTHON) Tests/test_install_lifecycle.py
	$(PYTHON) Tests/test_san.py
	$(PYTHON) Tests/test_explanations.py
	$(PYTHON) Tests/test_review.py
	$(PYTHON) Tests/test_study_store.py
	$(PYTHON) Tests/test_library_migration.py
	$(PYTHON) Tests/test_study.py
	$(PYTHON) Tests/test_pgn_import.py
	$(PYTHON) Tests/test_overlay.py
	CHESSLISTENER_HOST="$(abspath $(TARGET))" \
	CHESSLISTENER_FAKE_UCI="$(abspath Tests/fake_uci_engine.py)" \
		$(PYTHON) Tests/test_catchup.py
	CHESSLISTENER_HOST="$(abspath $(TARGET))" \
	CHESSLISTENER_FAKE_UCI="$(abspath Tests/fake_uci_engine.py)" \
		$(PYTHON) Tests/e2e.py
	CHESSLISTENER_HOST="$(abspath $(TARGET))" \
		$(PYTHON) Tests/test_engine_lanes.py

debug:
	$(MAKE) --no-print-directory -B \
		BUILD_DIR=.build/debug \
		TARGET=chess-listener-host-debug \
		CFLAGS='-std=c11 -O0 -g3 -Wall -Wextra -Wpedantic -pthread'

asan:
	$(MAKE) --no-print-directory -B \
		BUILD_DIR=.build/asan \
		TARGET=chess-listener-host-asan \
		CFLAGS='-std=c11 -O1 -g3 -Wall -Wextra -Wpedantic -pthread -fno-omit-frame-pointer -fsanitize=address,undefined' \
		LDFLAGS='-fsanitize=address,undefined'
	ASAN_OPTIONS='detect_leaks=0:halt_on_error=1' \
	UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1' \
	CHESSLISTENER_HOST="$(abspath chess-listener-host-asan)" \
	CHESSLISTENER_FAKE_UCI="$(abspath Tests/fake_uci_engine.py)" \
		$(PYTHON) Tests/test_catchup.py
	ASAN_OPTIONS='detect_leaks=0:halt_on_error=1' \
	UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1' \
	CHESSLISTENER_HOST="$(abspath chess-listener-host-asan)" \
	CHESSLISTENER_FAKE_UCI="$(abspath Tests/fake_uci_engine.py)" \
		$(PYTHON) Tests/e2e.py
	ASAN_OPTIONS='detect_leaks=0:halt_on_error=1' \
	UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1' \
	CHESSLISTENER_HOST="$(abspath chess-listener-host-asan)" \
		$(PYTHON) Tests/test_engine_lanes.py

# Focused race detector for the independent Stockfish/Maia owner threads.
# This is optional because ThreadSanitizer is not available in every toolchain.
tsan:
	$(MAKE) --no-print-directory -B \
		BUILD_DIR=.build/tsan \
		TARGET=chess-listener-host-tsan \
		CFLAGS='-std=c11 -O1 -g3 -Wall -Wextra -Wpedantic -pthread -fno-omit-frame-pointer -fsanitize=thread' \
		LDFLAGS='-fsanitize=thread'
	TSAN_OPTIONS='halt_on_error=1' \
	CHESSLISTENER_HOST="$(abspath chess-listener-host-tsan)" \
		$(PYTHON) Tests/test_engine_lanes.py

clean:
	rm -rf .build
	rm -f chess-listener-host chess-listener-host-debug chess-listener-host-asan
	rm -f chess-listener-host-tsan
	rm -f main.o uci.o overlay.o analysis.o
	rm -rf __pycache__ Tests/__pycache__

-include $(DEPS)
