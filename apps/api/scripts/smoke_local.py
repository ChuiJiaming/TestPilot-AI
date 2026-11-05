"""End-to-end smoke test against a locally running API.

Walks the real account lifecycle over HTTP: register -> read the verification
token out of the Outbox -> verify -> login -> call an authenticated endpoint.
The token is read from the database because no SMTP server is involved in a
local run; that is exactly what the Outbox is for.

Requires the API to be listening (see ``dev_server``) and the same
``TESTPILOT_DATABASE_URL`` / ``TESTPILOT_EMAIL_OUTBOX_ENCRYPTION_KEYS`` the API
was started with. ``.env.local`` is loaded automatically.

Usage::

    python -m apps.api.scripts.smoke_local --base-url http://127.0.0.1:8000
"""

import argparse
import os
import sys
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text

from apps.api.app.auth.adapters import VerificationTokenCipher

REPO_ROOT = Path(__file__).resolve().parents[3]


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{f'  ({detail})' if detail else ''}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--email", default="smoke@testpilot.local")
    parser.add_argument("--password", default="SmokeTestPassw0rd!")
    args = parser.parse_args()

    load_env_file(REPO_ROOT / ".env.local")
    database_url = os.environ.get("TESTPILOT_DATABASE_URL")
    keys = os.environ.get("TESTPILOT_EMAIL_OUTBOX_ENCRYPTION_KEYS")
    if not database_url or not keys:
        print("TESTPILOT_DATABASE_URL and TESTPILOT_EMAIL_OUTBOX_ENCRYPTION_KEYS must be set")
        return 2

    # trust_env=False: a workstation-wide HTTP_PROXY must not intercept loopback.
    client = httpx.Client(base_url=args.base_url, timeout=20, trust_env=False)
    failures = 0

    response = client.get("/api/v1/health/live")
    failures += not check("liveness", response.status_code == 200, response.text[:80])

    response = client.post(
        "/api/v1/auth/register",
        json={"email": args.email, "password": args.password, "display_name": "Smoke Test"},
    )
    failures += not check(
        "register accepted",
        response.status_code in {202, 409},
        f"HTTP {response.status_code}",
    )

    engine = create_engine(database_url)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "select token_ciphertext from verification_email_outbox "
                "where recipient_email = :email order by created_at desc limit 1"
            ),
            {"email": args.email},
        ).fetchone()
    failures += not check("verification token queued in outbox", row is not None)
    if row is None:
        return 1

    token = VerificationTokenCipher(keys).decrypt(row[0])
    response = client.post("/api/v1/auth/verify-email", json={"token": token})
    failures += not check(
        "email verified", response.status_code == 200, response.text[:120]
    )

    response = client.post(
        "/api/v1/auth/login",
        json={"email": args.email, "password": args.password, "device_label": "smoke"},
    )
    failures += not check("login", response.status_code == 200, response.text[:120])
    access_token = response.json().get("access_token") if response.status_code == 200 else None
    if not access_token:
        return 1

    response = client.get(
        "/api/v1/organizations", headers={"Authorization": f"Bearer {access_token}"}
    )
    failures += not check(
        "authenticated read", response.status_code == 200, response.text[:120]
    )

    print("\nall checks passed" if not failures else f"\n{failures} check(s) failed")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
