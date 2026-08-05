from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
METHOD_ROUTE_RE = re.compile(
    r"@(?:app|router)\.(get|post|put|patch|delete|options)\s*\(\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)


def _normalized(path: str) -> str:
    path = re.sub(r"\{[^}/]+\}", "{}", path)
    return re.sub(r"/+", "/", path).rstrip("/") or "/"


def test_exact_strict_route_contract_matches_app() -> None:
    contract = json.loads((ROOT / "evidence" / "route_contract.json").read_text())
    expected = {
        (row["method"].upper(), _normalized(row["path"]))
        for row in contract["routes"]
    }
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    actual = {
        (method.upper(), _normalized(path))
        for method, path in METHOD_ROUTE_RE.findall(source)
    }
    assert len(expected) == 37
    assert actual == expected
    assert contract["source_count"] == 37
    assert contract["target_count"] == 37
    assert contract["tested_count"] == 37
    assert contract["coverage"] == 1.0
    assert contract["omissions"] == []


def test_provenance_identities_are_distinct_and_pinned() -> None:
    migration = json.loads((ROOT / "migration_contract.json").read_text())
    contract = json.loads((ROOT / "evidence" / "route_contract.json").read_text())
    catalog = migration["provenance"]["canonical_catalog_source"]["sha256"]
    strict = migration["provenance"]["strict_recovered_bundle"]["sha256"]
    legacy = migration["provenance"]["repository_legacy_source"]["sha256"]
    assert catalog == "3466f4aa8d500ef4d4298b49c04166161dd9f42cfcc4044a1538f24ae8a5a521"
    assert strict == "08b06de2bf73c901798540b16184dbc77371b795c5a3c40094c76f825537d156"
    assert catalog != strict
    assert contract["contract_source_sha256"] == strict
    assert legacy == "5042929212518ddcf4d6799efceaac52361e817cc7acbf692bed10f476a76895"
    assert contract["source_route_extractor"] == "dispatch_conditions_v1"
    assert all(
        re.fullmatch(r"[0-9a-f]{64}", value) for value in (catalog, strict, legacy)
    )


def test_no_literal_secret_material_is_tracked() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in ROOT.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and "__pycache__" not in path.parts
        and path.suffix.lower()
        in {".py", ".json", ".md", ".sh", ".service", ".timer", ".sql", ".txt"}
        and path.stat().st_size < 1_000_000
    )
    # Assemble markers so the scanner fixture does not match its own source.
    forbidden = (
        "-----BEGIN " + "PRIVATE KEY-----",
        "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
        "sk_" + "live_",
        "wh" + "sec_",
    )
    for marker in forbidden:
        assert marker not in text


def test_legacy_source_is_preserved_unchanged() -> None:
    # This is the Git repository's historical TypeScript artifact, not either
    # recovered deployed artifact. Keeping its independent identity prevents a
    # migration commit from silently rewriting provenance.
    data = (ROOT / "src" / "index.ts").read_bytes().replace(b"\r\n", b"\n")
    digest = hashlib.sha256(data).hexdigest()
    assert digest == "5042929212518ddcf4d6799efceaac52361e817cc7acbf692bed10f476a76895"


def test_recovered_state_contract_preserves_the_single_widget() -> None:
    migration = json.loads((ROOT / "migration_contract.json").read_text())
    recovery = migration["state_recovery"]
    assert recovery["indexed_empty_application_exports"] == 10
    assert recovery["indexed_nonempty_application_exports"] == 1
    assert recovery["recovered_payload_rows"] == 1
    assert "legacy_*_text_v1" in recovery["policy"]


def test_smoke_uses_canonical_widget_route_placeholders() -> None:
    smoke = (ROOT / "smoke_live.py").read_text(encoding="utf-8")
    assert 'contract_path="/widgets/{{}}"' not in smoke
    assert smoke.count('contract_path="/widgets/{}"') == 4
