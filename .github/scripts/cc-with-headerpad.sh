#!/bin/bash
# Link wrapper: leave room for install_name_tool to add @rpath load commands.
# Compile steps (presence of -c / -E / -M / -MM) are passed through unchanged.
set -euo pipefail
link=1
for arg in "$@"; do
	case "$arg" in
		-c|-E|-M|-MM) link=0 ;;
	esac
done
if [[ "$link" == 1 ]]; then
	exec /usr/bin/clang -Wl,-headerpad_max_install_names -mmacosx-version-min="${MACOSX_DEPLOYMENT_TARGET:-11.0}" "$@"
else
	exec /usr/bin/clang "$@"
fi
