from __future__ import annotations

from register_public_route import HOSTNAME, SERVICE, desired_config


def test_public_route_precedes_wildcard_and_preserves_other_config() -> None:
    config = {
        "ingress": [
            {"hostname": "api.echo-op.com", "service": "http://127.0.0.1:8000"},
            {"hostname": "*.echo-op.com", "service": "http://127.0.0.1:8212"},
            {"service": "http_status:404"},
        ],
        "warp-routing": {"enabled": False},
    }
    updated, index, unchanged = desired_config(config)
    assert not unchanged
    assert index == 1
    assert updated["ingress"][index] == {"hostname": HOSTNAME, "service": SERVICE}
    assert updated["warp-routing"] == config["warp-routing"]


def test_public_route_update_is_idempotent_and_removes_stale_duplicate() -> None:
    config = {
        "ingress": [
            {"hostname": HOSTNAME, "service": SERVICE},
            {"hostname": "*.echo-op.com", "service": "http://127.0.0.1:8212"},
            {"service": "http_status:404"},
        ]
    }
    updated, index, unchanged = desired_config(config)
    assert unchanged
    assert index == 0
    assert updated == config

    stale = {
        "ingress": [
            {"hostname": HOSTNAME, "service": "http://127.0.0.1:9999"},
            {"hostname": HOSTNAME, "service": SERVICE},
            {"hostname": "*.echo-op.com", "service": "http://127.0.0.1:8212"},
            {"service": "http_status:404"},
        ]
    }
    corrected, _, unchanged = desired_config(stale)
    assert not unchanged
    assert sum(row.get("hostname") == HOSTNAME for row in corrected["ingress"]) == 1
