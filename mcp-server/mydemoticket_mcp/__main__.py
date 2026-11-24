"""Entry point: python3 -m mydemoticket_mcp (speaks MCP over stdin/stdout)."""

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
