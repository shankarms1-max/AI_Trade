"""Offline image-only Commodity Compose proposal. Never invokes Docker or deploys.

Read a locally supplied Compose JSON and write a NEW private proposal file. Runtime
secrets are not displayed. The caller must separately verify zero active exposure
and production identity before any future operator-approved application.
"""

import argparse
import copy
import json
import os
from pathlib import Path
import re

TARGETS = ("backend", "crudeoilm", "natgasmini")
CORE = frozenset((*TARGETS, "postgres", "frontend", "gateway"))
WORKER_SOURCES = frozenset(
    {
        "/app/worker/commodity_acts_paper_worker.py",
        "/app/worker/commodity_acts_health.py",
        "/app/worker/commodity_session_verify.py",
        "/app/worker/commodity_session_monitor.py",
    }
)


def _target(volume):
    if isinstance(volume, dict):
        target = volume.get("target")
        if not isinstance(target, str) or not target.startswith("/"):
            raise ValueError("COMMODITY_MOUNT_TARGET_INVALID")
        return target, volume.get("type") == "bind", volume.get("read_only") is True
    if isinstance(volume, str):
        fields = volume.split(":")
        if len(fields) in (2, 3):
            return fields[1], fields[0].startswith("/"), len(fields) == 3 and "ro" in fields[2].split(",")
    raise ValueError("COMMODITY_MOUNT_SYNTAX_UNSUPPORTED")


def image_model(original, image):
    if not re.fullmatch(r"ghcr\.io/shankarms1-max/ai_trade/backend@sha256:[0-9a-f]{64}", image):
        raise ValueError("COMMODITY_PINNED_BACKEND_DIGEST_REQUIRED")
    if original.get("name") not in (None, "commodity_acts") or set(original.get("services", {})) != CORE:
        raise ValueError("COMMODITY_PROJECT_OR_SERVICES_MISMATCH")
    result = copy.deepcopy(original)
    removed = {}
    for name in TARGETS:
        row = result["services"][name]
        if row.get("restart") != "unless-stopped" or "build" in row:
            raise ValueError("COMMODITY_RESTART_OR_BUILD_CONFIGURATION_UNSUPPORTED")
        memory = row.get("mem_limit")
        expected = 384 if name == "backend" else 512
        if memory not in (expected * 1024**2, f"{expected}m", f"{expected}M"):
            raise ValueError("COMMODITY_MEMORY_LIMIT_UNVERIFIED")
        kept, removed[name] = [], []
        for volume in row.get("volumes", []):
            target, bind, readonly = _target(volume)
            code = (
                target == "/app/backend/app/commodity_acts"
                or target.startswith("/app/backend/app/commodity_acts/")
                or target in WORKER_SOURCES
            )
            if code:
                if not bind or not readonly:
                    raise ValueError("COMMODITY_CODE_MOUNT_NOT_READ_ONLY_BIND")
                removed[name].append(target)
            else:
                if target == "/app" or target.startswith(("/app/backend", "/app/worker", "/app/scripts")):
                    raise ValueError("COMMODITY_UNRECOGNIZED_SOURCE_MOUNT")
                kept.append(volume)
        row["volumes"] = kept
        row["image"] = image
        before, after = copy.deepcopy(original["services"][name]), copy.deepcopy(row)
        for key in ("image", "volumes"):
            before.pop(key, None)
            after.pop(key, None)
        if before != after:
            raise ValueError("COMMODITY_RUNTIME_CONFIGURATION_CHANGED")
    for name in CORE - set(TARGETS):
        if result["services"][name] != original["services"][name]:
            raise ValueError("COMMODITY_PROTECTED_SERVICE_CHANGED")
    return result, removed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-file", required=True, type=Path)
    parser.add_argument("--backend-image", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        original = json.loads(args.compose_file.read_text())
        proposed, removed = image_model(original, args.backend_image)
        # Exclusive creation: never overwrite an existing Compose/production file.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(proposed, indent=2) + "\n")
    except (ValueError, TypeError, OSError, KeyError):
        parser.error("COMMODITY_IMAGE_PLAN_REFUSED; input or private output invalid")
    print(
        json.dumps(
            {
                "mode": "OFFLINE_PLAN_ONLY",
                "targets": TARGETS,
                "removed_code_targets": removed,
                "output": str(args.output),
                "deploy": False,
                "migrations": False,
                "requires_zero_active_exposure_before_apply": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
