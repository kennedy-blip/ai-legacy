"""The read-only instance that serves the bundled demo (the configuration deployed to Vercel)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.demo import DEMO_REPOSITORY_ID
from backend.main import API_PREFIX

from .conftest import REPOSITORY


def _json(client: TestClient, path: str, **params: Any) -> Any:
    response = client.get(path, params=params)
    assert response.status_code == 200, f"{path}: {response.status_code} {response.text[:200]}"
    return response.json()


def _node(client: TestClient, query: str, label: str) -> dict[str, Any]:
    results = _json(client, f"{REPOSITORY}/search", q=query, labels=label)["results"]
    assert results, f"nothing found for {query!r}"
    return results[0]


def _explain(client: TestClient, question: str, node_id: str | None = None) -> list[dict[str, Any]]:
    body = {"question": question, "focus_node_id": node_id, "stream": True}
    response = client.post(f"{REPOSITORY}/explain", json=body)
    assert response.status_code == 200, response.text[:200]
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


# ------------------------------------------------------------------------------ instance


def test_health_reports_components_without_details(hosted: TestClient) -> None:
    health = _json(hosted, f"{API_PREFIX}/health")
    assert health["status"] == "ok"
    # Unauthenticated endpoint: status only, nothing about the host.
    assert health["components"] == {
        "graph": {"status": "ok"},
        "vectors": {"status": "ok"},
        "llm": {"status": "disabled"},
    }


def test_system_describes_a_read_only_instance(hosted: TestClient) -> None:
    system = _json(hosted, f"{API_PREFIX}/system")
    assert system["read_only"] is True
    assert system["vector_backend"] == "memory"
    assert system["answer_mode"] == "graph-report"
    assert "/" not in json.dumps(system["components"]), "component details must not expose paths"


def test_demo_is_listed_as_a_snapshot(hosted: TestClient) -> None:
    repositories = _json(hosted, f"{API_PREFIX}/repositories")
    assert [repository["id"] for repository in repositories] == [DEMO_REPOSITORY_ID]
    demo = repositories[0]
    assert demo["is_demo"] is True
    assert demo["from_snapshot"] is True
    assert demo["status"] == "ready"
    assert (demo["files"], demo["functions"], demo["tables"]) == (25, 72, 8)


def test_unknown_repository_is_404(hosted: TestClient) -> None:
    assert hosted.get(f"{API_PREFIX}/repositories/nope").status_code == 404
    assert hosted.get(f"{API_PREFIX}/repositories/nope/topology").status_code == 404


# -------------------------------------------------------------------------------- graph


def test_entry_points_cover_http_scripts_and_schedules(hosted: TestClient) -> None:
    entries = _json(hosted, f"{REPOSITORY}/entry-points")["entry_points"]
    routes = {f"{entry['http_method']} {entry['route']}" for entry in entries if entry.get("http_method")}
    assert routes == {"GET /accounts/{id}/balance", "GET /invoices/{id}", "POST /invoices/{id}/pay"}
    assert {entry["entry_kind"] for entry in entries} == {"http", "main", "scheduled"}
    assert all(entry["is_entry_point"] for entry in entries)


def test_search_by_name_location_and_vocabulary(hosted: TestClient) -> None:
    by_name = _json(hosted, f"{REPOSITORY}/search", q="lateFee")["results"]
    assert [(hit["qualified_name"], hit["match"]) for hit in by_name] == [("FeeCalculator.lateFee", "name")]

    by_location = _json(hosted, f"{REPOSITORY}/search", q="FeeCalculator.java:25")["results"]
    assert by_location[0]["qualified_name"] == "FeeCalculator.lateFee"
    assert by_location[0]["match"] == "location"

    # Two words are not a name; the in-memory index, rebuilt from the snapshot, finds them.
    assert _json(hosted, f"{REPOSITORY}/search", q="late fee")["results"] == []
    by_words = _json(hosted, f"{REPOSITORY}/search", q="late fee", semantic=True)["results"]
    assert by_words[0]["qualified_name"] == "FeeCalculator.lateFee"
    assert by_words[0]["match"] == "semantic"


def test_source_text_is_served_from_the_bundled_files(hosted: TestClient) -> None:
    pay = _node(hosted, "InvoiceController.pay", "Function")
    source = _json(hosted, f"{REPOSITORY}/nodes/{pay['id']}/source")
    assert source["path"].endswith("InvoiceController.java")
    assert "paymentService.applyPayment(id, amount, reference)" in source["text"]
    assert source["focus"] == {"start_line": pay["start_line"], "end_line": pay["end_line"]}


def test_source_cannot_leave_the_repository(hosted: TestClient) -> None:
    for path in ("../../etc/passwd", "/etc/passwd", "src/../../../etc/passwd"):
        response = hosted.get(f"{REPOSITORY}/source", params={"path": path})
        assert response.status_code in (400, 404), f"{path}: {response.status_code}"


def test_node_context_of_a_table_includes_its_trigger(hosted: TestClient) -> None:
    ledger = _node(hosted, "tbl_ledger", "DatabaseTable")
    context = _json(hosted, f"{REPOSITORY}/nodes/{ledger['id']}")
    assert [item["node"]["name"] for item in context["triggers"]] == ["trg_ledger_audit"]
    writers = {
        item["node"].get("function_name") or item["node"]["qualified_name"] for item in context["writers"]
    }
    assert writers == {"LedgerDao.insertEntry", "sp_apply_payment", "sp_close_period"}
    assert hosted.get(f"{REPOSITORY}/nodes/nope").status_code == 404


def test_commit_lists_the_code_it_still_owns(hosted: TestClient) -> None:
    hits = _json(hosted, f"{REPOSITORY}/search", q="BILL-1432 late fees", labels="Commit", semantic=True)
    commit = hits["results"][0]
    context = _json(hosted, f"{REPOSITORY}/nodes/{commit['id']}")
    assert "BILL-1432" in context["node"]["message"]
    owned = {item["node"].get("qualified_name") for item in context["touched"]}
    assert "FeeCalculator.lateFee" in owned


def test_sequence_follows_the_payment_request(hosted: TestClient) -> None:
    pay = _node(hosted, "InvoiceController.pay", "Function")
    sequence = _json(hosted, f"{REPOSITORY}/sequence", node_id=pay["id"])
    assert sequence["root"]["qualified_name"] == "InvoiceController.pay"
    assert sequence["participants"][0]["name"] == "InvoiceController"
    assert sequence["steps"], "the payment flow has calls"
    assert {"tbl_ledger", "tbl_invoice"} <= {participant["name"] for participant in sequence["participants"]}


def test_sequence_refuses_a_table(hosted: TestClient) -> None:
    ledger = _node(hosted, "tbl_ledger", "DatabaseTable")
    assert hosted.get(f"{REPOSITORY}/sequence", params={"node_id": ledger["id"]}).status_code == 422


def test_lineage_reaches_the_hidden_audit_table(hosted: TestClient) -> None:
    ledger = _node(hosted, "tbl_ledger", "DatabaseTable")
    lineage = _json(hosted, f"{REPOSITORY}/lineage", node_id=ledger["id"])
    names = {node["id"]: node.get("qualified_name") or node["name"] for node in lineage["nodes"]}
    side_effects = {names[node_id] for node_id, layer in lineage["layers"].items() if layer == "side_effect"}
    assert {"trg_ledger_audit", "fn_ledger_audit", "tbl_ledger_audit"} <= side_effects
    entries = {names[node_id] for node_id, layer in lineage["layers"].items() if layer == "entry"}
    assert "InvoiceController.pay" in entries


def test_topology_groups_code_and_tables(hosted: TestClient) -> None:
    topology = _json(hosted, f"{REPOSITORY}/topology")
    kinds = [group["kind"] for group in topology["groups"]]
    assert set(kinds) == {"directory", "table"}
    assert kinds.count("table") == 8
    known = {group["id"] for group in topology["groups"]}
    assert topology["links"]
    assert all(link["source"] in known and link["target"] in known for link in topology["links"])


def test_runtime_overlay_is_labelled_simulated(hosted: TestClient) -> None:
    summary = _json(hosted, f"{REPOSITORY}/telemetry/summary")
    assert summary["simulated"] is True
    assert summary["sources"] == ["simulated"]
    totals = summary["totals"]
    assert totals["confirmed_static_calls"] <= totals["static_calls"]
    assert totals["invocations"] > 0


# ------------------------------------------------------------------------------ answers


def test_explain_streams_a_grounded_report(hosted: TestClient) -> None:
    ledger = _node(hosted, "tbl_ledger", "DatabaseTable")
    events = _explain(
        hosted, "Which code writes to tbl_ledger, and are there hidden side effects?", ledger["id"]
    )
    kinds = [event["type"] for event in events]
    assert kinds[0] == "context"
    assert kinds[-2:] == ["citations", "done"]
    assert "token" in kinds
    assert "error" not in kinds

    summary = events[-2]["summary"]
    assert summary["total"] > 0
    assert summary["verified"] == summary["total"], events[-2]
    assert summary["grounded"] is True

    answer = events[-1]["answer"]
    assert "trg_ledger_audit" in answer
    assert "tbl_ledger_audit" in answer


def test_explain_names_each_reader_and_writer_once(hosted: TestClient) -> None:
    """One access is stored on the function, on its SQL statement and as a runtime observation."""
    ledger = _node(hosted, "tbl_ledger", "DatabaseTable")
    answer = _explain(hosted, "Who reads and writes tbl_ledger?", ledger["id"])[-1]["answer"]
    lines = [line.strip() for line in answer.splitlines()]
    for owner in ("LedgerDao.existsByReference", "fetch_page", "NightlyReconciliationJob.reconcile"):
        assert sum(line.startswith(f"- read by {owner} ") for line in lines) == 1, owner
    for owner in ("LedgerDao.insertEntry", "sp_apply_payment", "sp_close_period"):
        assert sum(line.startswith(f"- written by {owner} ") for line in lines) == 1, owner
    # The citation points at the statement, not at the top of the function.
    assert "- written by LedgerDao.insertEntry (INSERT) [LedgerDao.java:16]" in lines


def test_explain_without_streaming_returns_one_document(hosted: TestClient) -> None:
    body = {"question": "Why is the late fee 0.05% per day?", "stream": False}
    response = hosted.post(f"{REPOSITORY}/explain", json=body)
    assert response.status_code == 200
    result = response.json()
    assert result["mode"] == "graph-report"
    assert result["citation_summary"]["unresolved"] == 0
    assert result["citation_summary"]["total"] == len(result["citations"])
    # The reason lives in a commit message, so the commit must be among the evidence.
    assert any(item["kind"] == "commit" and "BILL-1432" in item["text"] for item in result["evidence"])
    assert "[commit 64c96ce8]" in result["answer"]


def test_explain_rejects_an_empty_question(hosted: TestClient) -> None:
    assert hosted.post(f"{REPOSITORY}/explain", json={"question": ""}).status_code == 422


# ---------------------------------------------------------------------------- read-only


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", f"{API_PREFIX}/repositories", {"source": "https://github.com/example/repository"}),
        ("DELETE", REPOSITORY, None),
        ("POST", f"{REPOSITORY}/reingest", None),
        ("POST", f"{REPOSITORY}/telemetry/events", {"events": []}),
        ("POST", f"{REPOSITORY}/telemetry/otlp", {}),
        ("POST", f"{REPOSITORY}/telemetry/simulate", {"traces": 5}),
        ("DELETE", f"{REPOSITORY}/telemetry", None),
    ],
)
def test_every_write_is_refused(hosted: TestClient, method: str, path: str, body: Any) -> None:
    response = hosted.request(method, path, json=body)
    assert response.status_code == 403, f"{method} {path}: {response.status_code}"
    assert "read-only" in response.json()["detail"]


def test_refused_writes_leave_the_graph_untouched(hosted: TestClient) -> None:
    before = _json(hosted, f"{REPOSITORY}/telemetry/summary")["totals"]
    hosted.post(f"{REPOSITORY}/telemetry/simulate", json={"traces": 50})
    hosted.delete(REPOSITORY)
    assert _json(hosted, f"{REPOSITORY}/telemetry/summary")["totals"] == before
    assert len(_json(hosted, f"{API_PREFIX}/repositories")) == 1
