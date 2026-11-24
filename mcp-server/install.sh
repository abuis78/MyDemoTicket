#!/bin/sh
# Register (or remove) the MyDemoTicket MCP server in Claude Code.
#
#   ./install.sh [--dry-run] [--uninstall] [--scope user|project] [--name mydemoticket]
#
# Without the `claude` CLI it prints the snippet for Claude Desktop instead.
# Running it again replaces the existing registration (no duplicates).
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUN_SH="$SCRIPT_DIR/run.sh"
DRY_RUN=0
UNINSTALL=0
SCOPE=user
NAME=mydemoticket

usage() {
    sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --uninstall) UNINSTALL=1 ;;
        --scope) [ $# -ge 2 ] || { echo "error: --scope needs a value (user or project)" >&2; exit 2; }
                 SCOPE=$2; shift ;;
        --scope=*) SCOPE=${1#--scope=} ;;
        --name) [ $# -ge 2 ] || { echo "error: --name needs a value" >&2; exit 2; }
                NAME=$2; shift ;;
        --name=*) NAME=${1#--name=} ;;
        -h|--help) usage; exit 0 ;;
        *) echo "error: unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

case "$SCOPE" in
    user|project) ;;
    *) echo "error: --scope must be user or project (got: $SCOPE)" >&2; exit 2 ;;
esac
case "$NAME" in
    ''|*[!A-Za-z0-9_-]*) echo "error: --name may only contain letters, digits, - and _" >&2; exit 2 ;;
esac

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[dry-run] $*"
    else
        "$@"
    fi
}

if ! command -v claude >/dev/null 2>&1; then
    echo "The Claude Code CLI (claude) is not installed, so nothing was registered."
    if [ "$UNINSTALL" -eq 1 ]; then
        echo "For Claude Desktop, remove the \"$NAME\" entry under \"mcpServers\" from"
        echo "  ~/Library/Application Support/Claude/claude_desktop_config.json (macOS)"
        echo "and restart Claude Desktop."
    else
        echo "For Claude Desktop, add this to \"mcpServers\" in"
        echo "  ~/Library/Application Support/Claude/claude_desktop_config.json (macOS)"
        echo "and restart Claude Desktop:"
        echo
        printf '{\n  "mcpServers": {\n    "%s": {\n      "command": "%s"\n    }\n  }\n}\n' "$NAME" "$RUN_SH"
        echo
        echo "Install Claude Code (https://claude.com/claude-code) and run this script again to register it there."
    fi
    exit 0
fi

if [ "$UNINSTALL" -eq 1 ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        run claude mcp remove --scope "$SCOPE" "$NAME"
        echo "Dry run only: nothing was changed."
    elif claude mcp remove --scope "$SCOPE" "$NAME"; then
        echo "Removed MCP server \"$NAME\" ($SCOPE scope) from Claude Code."
    else
        echo "MCP server \"$NAME\" was not registered in $SCOPE scope; nothing to remove."
    fi
    exit 0
fi

chmod +x "$RUN_SH" 2>/dev/null || true
# Replace an existing registration instead of failing or duplicating it.
if [ "$DRY_RUN" -eq 1 ]; then
    echo "[dry-run] claude mcp remove --scope $SCOPE $NAME   (only if it already exists)"
else
    claude mcp remove --scope "$SCOPE" "$NAME" >/dev/null 2>&1 || true
fi
# The name goes before -e: -e takes several values and would swallow it.
set -- claude mcp add --scope "$SCOPE" "$NAME"
if [ -n "${MYDEMOTICKET_URL:-}" ]; then
    set -- "$@" -e "MYDEMOTICKET_URL=$MYDEMOTICKET_URL"
fi
set -- "$@" -- "$RUN_SH"
run "$@"
if [ "$DRY_RUN" -eq 1 ]; then
    echo "Dry run only: nothing was changed."
else
    echo "Registered MCP server \"$NAME\" ($SCOPE scope) in Claude Code."
    echo "Next: store the API token in the keychain (see README.md), then check with: claude mcp list"
fi
