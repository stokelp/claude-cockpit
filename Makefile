# Claude Cockpit
#   make            build the app into build/
#   make install    build, copy to ~/Applications, install the Claude Code hooks, open it
#   make uninstall  remove the hooks and the app (judge verdicts in ~/.claude/cockpit stay)
#   make test       Python unit tests
#   make screenshot regenerate docs/screenshot*.png from sample sessions

APP_NAME   := Claude Cockpit
BUNDLE_ID  := com.stokelp.claude-cockpit
BUILD      := build
APP        := $(BUILD)/$(APP_NAME).app
DEST_DIR   := $(HOME)/Applications
DEST       := $(DEST_DIR)/$(APP_NAME).app
ARCH       := $(shell uname -m)
SOURCES    := $(wildcard app/Sources/*.swift)
PYTHON     := $(shell for p in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do [ -x $$p ] && echo $$p && break; done)

.PHONY: all app install uninstall test lint screenshot clean

all: app

app: $(SOURCES) app/Info.plist cockpit/cockpit.py
	@rm -rf "$(APP)"
	@mkdir -p "$(APP)/Contents/MacOS" "$(APP)/Contents/Resources"
	xcrun swiftc -parse-as-library -swift-version 5 -O -target $(ARCH)-apple-macos14.0 \
		$(SOURCES) -o "$(APP)/Contents/MacOS/ClaudeCockpit"
	cp app/Info.plist "$(APP)/Contents/Info.plist"
	cp cockpit/cockpit.py "$(APP)/Contents/Resources/cockpit.py"
	codesign --force --sign - --identifier $(BUNDLE_ID) "$(APP)"

install: app
	-osascript -e 'tell application id "$(BUNDLE_ID)" to quit' >/dev/null 2>&1
	@mkdir -p "$(DEST_DIR)"
	rm -rf "$(DEST)"
	ditto "$(APP)" "$(DEST)"
	$(PYTHON) "$(DEST)/Contents/Resources/cockpit.py" install-hooks
	open "$(DEST)"

uninstall:
	-[ -f "$(DEST)/Contents/Resources/cockpit.py" ] && $(PYTHON) "$(DEST)/Contents/Resources/cockpit.py" uninstall-hooks
	-osascript -e 'tell application id "$(BUNDLE_ID)" to quit' >/dev/null 2>&1
	rm -rf "$(DEST)"

test:
	$(PYTHON) -m unittest discover -s tests -v

lint:
	ruff format --check cockpit tests
	ruff check cockpit tests

screenshot: app
	COCKPIT_SCRIPT=cockpit/cockpit.py "$(APP)/Contents/MacOS/ClaudeCockpit" --render docs/screenshot.png --demo
	COCKPIT_SCRIPT=cockpit/cockpit.py "$(APP)/Contents/MacOS/ClaudeCockpit" --render docs/screenshot-dark.png --demo --dark

clean:
	rm -rf "$(BUILD)"
