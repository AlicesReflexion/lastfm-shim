"""Command line: ``python -m lfmshim <command>``."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from . import __version__
from .config import ConfigError, load_config


def _app(args):
    from .app import App

    return App(load_config(args.config))


def cmd_serve(args) -> int:
    from .server import ShimServer

    app = _app(args)
    logging.basicConfig(level=app.config.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    srv = ShimServer(app)
    if hasattr(app, "start_background"):
        app.start_background()
    print(f"lfmshim {__version__} listening on {srv.url}/2.0/ (public URL {app.config.server.effective_public_url()})",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        app.close()
    return 0


def _read_password(args) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Repeat password: "):
        raise SystemExit("passwords do not match")
    return pw


def cmd_adduser(args) -> int:
    app = _app(args)
    pw = None if args.no_password else _read_password(args)
    if pw is not None and len(pw) < 8:
        raise SystemExit("password must be at least 8 characters")
    app.store.add_user(args.name, pw)
    print(f"user {args.name} saved")
    return 0


def cmd_revoke(args) -> int:
    app = _app(args)
    print(f"revoked {app.store.revoke_sessions(args.name)} session(s) for {args.name}")
    return 0


def cmd_users(args) -> int:
    app = _app(args)
    for row in app.store.list_users():
        print(row["name"], "(password set)" if row["password_hash"] else "(no password)")
    return 0


def cmd_methods(args) -> int:
    from .app import App  # noqa: F401  (registers methods)
    from . import api  # noqa: F401
    from .api.registry import REGISTRY

    for spec in sorted(REGISTRY.values(), key=lambda s: s.name.lower()):
        print(f"{spec.name:32} {spec.support:12} {spec.notes}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lfmshim", description="Local Last.fm-compatible API shim")
    p.add_argument("--config", help="path to config TOML (default: $LFMSHIM_CONFIG)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the server").set_defaults(func=cmd_serve)
    a = sub.add_parser("adduser", help="create a local user / set its password")
    a.add_argument("name")
    a.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    a.add_argument("--no-password", action="store_true", help="create without a password (session adoption only)")
    a.set_defaults(func=cmd_adduser)
    r = sub.add_parser("revoke", help="revoke all sessions of a user")
    r.add_argument("name")
    r.set_defaults(func=cmd_revoke)
    sub.add_parser("users", help="list local users").set_defaults(func=cmd_users)
    sub.add_parser("methods", help="list implemented API methods").set_defaults(func=cmd_methods)
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
