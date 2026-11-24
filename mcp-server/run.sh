#!/bin/sh
# Start the MyDemoTicket MCP server (stdio). Used by Claude Code / Claude Desktop.
# If MYDEMOTICKET_TOKEN is empty and the macOS `security` tool exists, the token is
# read at runtime from the keychain item service "mydemoticket",
# account ${MYDEMOTICKET_KEYCHAIN_ACCOUNT:-mcp-api-token}. Nothing is printed to stdout.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ -z "${MYDEMOTICKET_TOKEN:-}" ] && command -v security >/dev/null 2>&1; then
    if token=$(security find-generic-password -s mydemoticket \
            -a "${MYDEMOTICKET_KEYCHAIN_ACCOUNT:-mcp-api-token}" -w 2>/dev/null); then
        MYDEMOTICKET_TOKEN=$token
        export MYDEMOTICKET_TOKEN
    else
        echo "mydemoticket-mcp: no keychain item (service mydemoticket, account ${MYDEMOTICKET_KEYCHAIN_ACCOUNT:-mcp-api-token}); tool calls will fail until a token is set" >&2
    fi
    unset token
fi

PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec python3 -m mydemoticket_mcp
