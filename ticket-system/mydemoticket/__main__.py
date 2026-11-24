"""Command line entry point: python3 -m mydemoticket --host 127.0.0.1 --port 8080."""

import argparse
import os
import sys

from . import __version__
from .db import default_data_dir
from .server import App


def main(argv=None):
    parser = argparse.ArgumentParser(prog="mydemoticket", description="MyDemoTicket ticketing server")
    parser.add_argument("--host", default=os.environ.get("MYDEMOTICKET_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MYDEMOTICKET_PORT", "8080")))
    parser.add_argument("--data-dir", default=None,
                        help="data directory (default: $MYDEMOTICKET_DATA_DIR or ~/.mydemoticket)")
    parser.add_argument("--no-seed", action="store_true", help="do not create demo teams/users/fields")
    parser.add_argument("--secure-cookies", action="store_true",
                        default=os.environ.get("MYDEMOTICKET_SECURE_COOKIES") == "1",
                        help="mark session cookies Secure (use behind HTTPS)")
    parser.add_argument("--quiet", action="store_true", help="do not log requests")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    args = parser.parse_args(argv)

    data_dir = os.path.abspath(os.path.expanduser(args.data_dir)) if args.data_dir else default_data_dir()
    app = App(data_dir, seed=not args.no_seed, secure_cookies=args.secure_cookies, quiet=args.quiet)
    httpd = app.make_server(args.host, args.port)
    host, port = httpd.server_address[:2]
    sys.stderr.write("MyDemoTicket %s listening on http://%s:%d (data: %s)\n" % (__version__, host, port, data_dir))
    if app.credentials_file:
        sys.stderr.write("Initial credentials written to %s (mode 0600)\n" % app.credentials_file)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
