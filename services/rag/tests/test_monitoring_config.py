"""The shipped Prometheus config has to match the endpoint that exists.

Docker is not available in this environment, so these files cannot be validated
by running Prometheus. What *can* be checked mechanically is the part that
usually breaks: a rule referencing a series the service never emits, and a
scrape contract the endpoint does not accept.
"""

import re
from pathlib import Path

import yaml

from app.application.tree_metrics_service import flatten_metrics

MONITORING = Path(__file__).resolve().parents[1] / "monitoring"


def _load(name: str):
    return yaml.safe_load((MONITORING / name).read_text(encoding="utf-8"))


def _seconds(value: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)?", str(value).strip())
    assert match, f"unparsable duration: {value}"
    scale = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, None: 1}[match.group(2)]
    return float(match.group(1)) * scale


def _referenced_series(rules: dict) -> set[str]:
    referenced: set[str] = set()
    for group in rules["groups"]:
        for rule in group["rules"]:
            referenced.update(re.findall(r"\brag_tree_[a-z0-9_]+\b", rule["expr"]))
    return referenced


def test_every_alert_references_a_series_the_service_emits():
    rules = _load("rag-tree-alerts.yml")

    referenced = _referenced_series(rules)
    emitted = set(flatten_metrics({}))

    assert referenced, "规则文件没有引用任何序列"
    assert referenced <= emitted, f"规则引用了服务不存在的序列: {referenced - emitted}"


def test_alert_rules_are_well_formed():
    rules = _load("rag-tree-alerts.yml")

    for group in rules["groups"]:
        assert group["name"]
        for rule in group["rules"]:
            assert rule["alert"]
            assert rule["expr"].strip()
            assert rule["labels"]["severity"] in {"warning", "critical"}
            assert rule["annotations"]["summary"]


def test_scrape_job_uses_the_shape_the_endpoint_accepts():
    config = _load("prometheus-scrape.example.yml")

    job = config["scrape_configs"][0]
    assert job["job_name"] == "rag"
    assert job["metrics_path"] == "/metrics"
    # Prometheus cannot send a custom header, which is why /metrics accepts
    # Bearer in addition to X-RAG-Internal-Token.
    assert job["authorization"]["type"] == "Bearer"
    assert job["authorization"]["credentials_file"].endswith("rag-internal-token")


def test_the_scrape_timeout_leaves_room_for_a_real_snapshot():
    job = _load("prometheus-scrape.example.yml")["scrape_configs"][0]

    interval = _seconds(job["scrape_interval"])
    timeout = _seconds(job["scrape_timeout"])

    # A scrape re-runs a snapshot per active scope and measured ~9-10s with 15
    # scopes, so the timeout has to be comfortably above that and the interval
    # above the timeout (otherwise scrapes overlap themselves).
    assert timeout >= 20
    assert interval > timeout
