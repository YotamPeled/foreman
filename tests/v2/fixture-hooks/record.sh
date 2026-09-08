#!/bin/sh
# Fixture hook: appends the event it receives to $HOOK_RECORD_FILE.
cat >> "$HOOK_RECORD_FILE"
