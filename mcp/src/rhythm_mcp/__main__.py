"""Entry point: `python -m rhythm_mcp --transport stdio` (or `--transport http`)."""
from __future__ import annotations

import argparse


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="rhythm-mcp")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--stateless", action="store_true", default=True,
                        help="stateless HTTP: every request is independent (horizontal scale)")
    args = parser.parse_args()

    if args.transport == "stdio":
        from rhythm_mcp.server import build_server
        from rhythm.agent.registry import Runtime

        build_server(Runtime()).run(transport="stdio")
        return

    import uvicorn

    from rhythm_mcp.server import build_http_app

    app, _registry = build_http_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
