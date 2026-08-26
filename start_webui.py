from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Start Voxtry local form panel")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=48765)
    args = parser.parse_args()
    uvicorn.run("app:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
