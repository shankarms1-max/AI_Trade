#!/usr/bin/env python3
"""Read-only post-deployment checks. Never calls broker, AI, or order APIs."""
import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SENSITIVE_MARKERS = (
    "database_url", "password", "consumer_key", "authorization", "token",
    "chat_id", "totp", "mpin", "secret",
)
SECRET_ENV_NAMES = (
    "DATABASE_URL", "POSTGRES_PASSWORD", "KOTAK_CONSUMER_KEY",
    "KOTAK_MOBILE_NUMBER", "KOTAK_UCC", "KOTAK_TOTP_SECRET", "KOTAK_MPIN",
    "OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
)


def fetch(base_url: str, path: str, *, allow_not_found: bool = False):
    url = f"{base_url.rstrip('/')}{path}"
    try:
        with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=10) as response:
            body = response.read().decode("utf-8")
            return response.status, body
    except HTTPError as exc:
        if allow_not_found and exc.code == 404:
            return exc.code, exc.read().decode("utf-8")
        raise


def contains_sensitive_key(value) -> bool:
    if isinstance(value, dict):
        return any(
            any(marker in key.lower() for marker in SENSITIVE_MARKERS)
            or contains_sensitive_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(contains_sensitive_key(item) for item in value)
    return False


def contains_configured_secret(raw_body: str) -> bool:
    secrets = (
        value for name in SECRET_ENV_NAMES
        if (value := os.getenv(name, "").strip()) and len(value) >= 4
    )
    return any(secret in raw_body for secret in secrets)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url", default=os.getenv("DEPLOYMENT_BASE_URL", "http://127.0.0.1")
    )
    args = parser.parse_args()
    checks: list[tuple[str, bool, str]] = []
    try:
        status, health_text = fetch(args.base_url, "/api/health")
        health = json.loads(health_text)
        database = health.get("components", {}).get("database")
        checks.append(("backend_health", status == 200, f"HTTP {status}"))
        checks.append(("database", database == "HEALTHY", str(database)))
        checks.append(("health_secret_fields", not contains_sensitive_key(health), "safe schema"))
        checks.append(("health_secret_values", not contains_configured_secret(health_text), "not returned"))

        status, _ = fetch(args.base_url, "/api/snapshots/latest", allow_not_found=True)
        checks.append(("latest_snapshot_endpoint", status in {200, 404}, f"HTTP {status}"))
        status, _ = fetch(args.base_url, "/api/pipeline/latest", allow_not_found=True)
        checks.append(("pipeline_endpoint", status in {200, 404}, f"HTTP {status}"))
        status, frontend = fetch(args.base_url, "/")
        checks.append(("frontend", status == 200 and "<html" in frontend.lower(), f"HTTP {status}"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"DEPLOYMENT_SMOKE_FAILED error_type={type(exc).__name__}")
        return 2

    for name, passed, detail in checks:
        print(f"{name}={'PASS' if passed else 'FAIL'} detail={detail}")
    return 0 if all(passed for _, passed, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
