"""Local development launcher for the API.

Production starts uvicorn against a real PostgreSQL, Redis and SMTP setup. This
launcher exists so a single Windows workstation can exercise the whole API
without installing a Redis service:

* loads ``.env.local`` from the repository root (git-ignored) if present;
* probes ``TESTPILOT_REDIS_URL``; if nothing is listening there, it swaps in
  an in-process ``fakeredis`` so rate limiting and refresh-token bookkeeping
  still work;
* starts uvicorn without ``--reload``, because a reloaded subprocess would
  lose the Redis substitution.

Never use this in production: the Redis fallback is per-process state, so two
workers would each keep their own rate-limit counters.

Usage::

    python -m apps.api.scripts.dev_server --port 8000
"""

import argparse
import os
import socket
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[3]


def load_env_file(path: Path) -> None:
    """Minimal KEY=VALUE loader. Existing environment variables win."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and not os.environ.get(key):
            os.environ[key] = value


def redis_reachable(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme != "redis" or not parsed.hostname:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 6379), timeout=1):
            return True
    except OSError:
        return False


def install_redis_fallback(reason: str) -> None:
    import fakeredis
    import redis

    fake = fakeredis.FakeRedis(decode_responses=True)
    original = redis.Redis.from_url

    def from_url(url: str, **kwargs):  # noqa: ANN001, ARG001 - signature parity
        return fake

    redis.Redis.from_url = from_url
    print(f"[dev] Redis fallback active ({reason}); state is in-process only.")
    print(f"[dev] restore the real client with: redis.Redis.from_url = {original!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env.local"))
    args = parser.parse_args()

    load_env_file(Path(args.env_file))

    redis_url = os.environ.get("TESTPILOT_REDIS_URL")
    if not redis_url:
        os.environ["TESTPILOT_REDIS_URL"] = "redis://127.0.0.1:6379/0"
        install_redis_fallback("TESTPILOT_REDIS_URL is not set")
    elif not redis_reachable(redis_url):
        install_redis_fallback(f"nothing listening on {redis_url}")

    if not os.environ.get("TESTPILOT_DATABASE_URL"):
        raise SystemExit(
            "TESTPILOT_DATABASE_URL is required. Copy deploy/.env.example to "
            ".env.local and point it at your local PostgreSQL."
        )

    import uvicorn

    # The Redis substitution must happen before the module-level
    # create_default_app() call, so hand uvicorn a factory instead of an import
    # string and keep reload disabled.
    uvicorn.run(
        "apps.api.app.main:app",
        host=args.host,
        port=args.port,
        reload=False,
        log_level=os.environ.get("TESTPILOT_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
