"""Offline Compose proposal; no Docker daemon, production, DB or broker calls."""

from copy import deepcopy
import json
import os

import pytest

from scripts.plan_commodity_image_runtime import CORE, TARGETS, image_model, main

IMAGE = "ghcr.io/shankarms1-max/ai_trade/backend@sha256:" + "a" * 64


def compose():
    limits = {
        "backend": "384m",
        "crudeoilm": "512m",
        "natgasmini": "512m",
        "postgres": "256m",
        "frontend": "256m",
        "gateway": "64m",
    }
    return {
        "name": "commodity_acts",
        "volumes": {"commodity_postgres": {"external": True}},
        "networks": {"private": {"internal": True}},
        "services": {
            name: {
                "image": "old-" + name,
                "restart": "unless-stopped",
                "mem_limit": limits[name],
                "environment": {"PAPER_ONLY": "true"},
                "networks": ["private"],
                "volumes": (
                    ["commodity_postgres:/var/lib/postgresql/data"]
                    if name == "postgres"
                    else ["cache:/app/cache", "logs:/app/logs", "/private/kotak:/run/secrets/kotak:ro"]
                ),
            }
            for name in CORE
        },
    }


def test_image_plan_removes_only_commodity_code_and_preserves_runtime_and_services():
    source = compose()
    for name in TARGETS:
        source["services"][name]["volumes"] += [
            {
                "type": "bind",
                "source": "/srv/commodity-acts/releases/old/broker.py",
                "target": "/app/backend/app/commodity_acts/broker.py",
                "read_only": True,
            },
            "/srv/old/verify.py:/app/worker/commodity_session_verify.py:ro",
        ]
    before = deepcopy(source)
    proposed, removed = image_model(source, IMAGE)
    assert source == before
    assert proposed["volumes"] == before["volumes"] and proposed["networks"] == before["networks"]
    for name in CORE - set(TARGETS):
        assert proposed["services"][name] == before["services"][name]
    for name in TARGETS:
        row = proposed["services"][name]
        assert row["image"] == IMAGE and row["volumes"] == before["services"][name]["volumes"][:3]
        assert row["mem_limit"] == before["services"][name]["mem_limit"]
        assert len(removed[name]) == 2


@pytest.mark.parametrize(
    "problem", ["tag", "project", "extra_service", "build", "restart", "wide_mount", "writable_code", "memory"]
)
def test_ambiguous_or_nonisolated_plans_fail_closed(problem):
    source, image = compose(), IMAGE
    if problem == "tag":
        image = "ghcr.io/shankarms1-max/ai_trade/backend:latest"
    elif problem == "project":
        source["name"] = "ai_trade"
    elif problem == "extra_service":
        source["services"]["nifty"] = {}
    elif problem == "build":
        source["services"]["backend"]["build"] = "."
    elif problem == "restart":
        source["services"]["backend"]["restart"] = "always"
    elif problem == "wide_mount":
        source["services"]["backend"]["volumes"].append("/old:/app/backend:ro")
    elif problem == "memory":
        source["services"]["backend"]["mem_limit"] = "768m"
    else:
        source["services"]["backend"]["volumes"].append("/old:/app/backend/app/commodity_acts/broker.py:rw")
    with pytest.raises(ValueError):
        image_model(source, image)


def test_cli_creates_private_proposal_without_overwriting_input_or_output(tmp_path, capsys):
    source, output = tmp_path / "compose.json", tmp_path / "proposed.json"
    source.write_text(json.dumps(compose()))
    before = source.read_bytes()
    args = ["--compose-file", str(source), "--backend-image", IMAGE, "--output", str(output)]
    assert main(args) == 0 and source.read_bytes() == before
    assert os.stat(output).st_mode & 0o777 == 0o600
    assert json.loads(capsys.readouterr().out)["deploy"] is False
    with pytest.raises(SystemExit):
        main(args)
    with pytest.raises(SystemExit):
        main(args + ["--apply"])
    assert source.read_bytes() == before
