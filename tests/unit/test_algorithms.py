"""Graph algorithms, exercised on the ingested demo monolith."""

from __future__ import annotations

from graph_engine import algorithms, locator
from graph_engine.schema import NodeLabel, RelType
from tests.conftest import Corpus


def names(corpus: Corpus, node_ids: object) -> set[str]:
    return {node.qualified_name for node in corpus.store.get_nodes(list(node_ids))}  # type: ignore[call-overload]


# --------------------------------------------------------------------------- call chain


def test_call_chain_downstream_reaches_tables_through_callees(corpus: Corpus) -> None:
    root = corpus.function("PaymentService.applyPayment")
    chain = algorithms.call_chain(corpus.store, root.id, direction="downstream", max_hops=3)
    assert chain is not None
    reached = {node.qualified_name for node in chain.graph.nodes.values()}
    assert {"LedgerDao.insertEntry", "InvoiceDao.markPaid", "sp_apply_payment", "tbl_ledger"} <= reached
    assert "InvoiceController.pay" not in reached  # a caller, not a callee
    assert chain.levels[root.id] == 0
    assert chain.levels[corpus.function("LedgerDao.insertEntry").id] == 1
    assert chain.levels[corpus.table("tbl_ledger").id] > 1


def test_call_chain_upstream_finds_the_http_entry_point(corpus: Corpus) -> None:
    root = corpus.function("LedgerDao.insertEntry")
    chain = algorithms.call_chain(corpus.store, root.id, direction="upstream", max_hops=3, include_db=False)
    assert chain is not None
    reached = {node.qualified_name for node in chain.graph.nodes.values()}
    assert {"PaymentService.applyPayment", "InvoiceController.pay"} <= reached
    assert all(node.label is NodeLabel.FUNCTION for node in chain.graph.nodes.values())
    assert chain.levels[corpus.function("InvoiceController.pay").id] == -2


def test_call_chain_respects_hop_limit(corpus: Corpus) -> None:
    root = corpus.function("InvoiceController.pay")
    one = algorithms.call_chain(corpus.store, root.id, direction="downstream", max_hops=1, include_db=False)
    two = algorithms.call_chain(corpus.store, root.id, direction="downstream", max_hops=2, include_db=False)
    assert one is not None and two is not None
    first = {node.qualified_name for node in one.graph.nodes.values()}
    assert "PaymentService.applyPayment" in first
    assert "LedgerDao.insertEntry" not in first
    assert "LedgerDao.insertEntry" in {node.qualified_name for node in two.graph.nodes.values()}
    assert max(one.levels.values()) == 1


def test_call_chain_of_a_class_starts_from_its_methods(corpus: Corpus) -> None:
    cls = corpus.node("FeeCalculator", NodeLabel.CLASS)
    chain = algorithms.call_chain(corpus.store, cls.id, direction="both", max_hops=1)
    assert chain is not None
    assert names(corpus, chain.start_ids) >= {"FeeCalculator.lateFee", "FeeCalculator.round"}
    assert all(chain.levels[start] == 0 for start in chain.start_ids)


def test_call_chain_of_a_table_lists_its_writers(corpus: Corpus) -> None:
    table = corpus.table("tbl_ledger")
    chain = algorithms.call_chain(corpus.store, table.id, direction="upstream", max_hops=2)
    assert chain is not None
    reached = {node.qualified_name for node in chain.graph.nodes.values()}
    assert {"LedgerDao.insertEntry", "sp_apply_payment"} <= reached


def test_call_chain_unknown_node(corpus: Corpus) -> None:
    assert algorithms.call_chain(corpus.store, "fn_00000000000000000000") is None


# ----------------------------------------------------------------------------- sequence


def test_sequence_exposes_the_trigger_behind_a_payment(corpus: Corpus) -> None:
    root = corpus.function("InvoiceController.pay")
    diagram = algorithms.sequence(corpus.store, root.id)
    assert diagram is not None
    participants = {participant.name: participant.kind for participant in diagram.participants}
    assert participants["InvoiceController"] == "class"
    assert participants["tbl_ledger"] == "table"
    kinds = [step.kind for step in diagram.steps]
    assert {"call", "read", "write", "trigger"} <= set(kinds)

    lifeline = {participant.id: participant.name for participant in diagram.participants}
    trigger = next(step for step in diagram.steps if step.kind == "trigger")
    assert "trg_ledger_audit" in trigger.label and lifeline[trigger.source] == "tbl_ledger"
    # The audit insert happens after, and nested inside, the trigger firing.
    audit = next(
        step for step in diagram.steps if step.kind == "write" and lifeline[step.target] == "tbl_ledger_audit"
    )
    assert audit.index > trigger.index and audit.depth > trigger.depth
    assert audit.label == "INSERT"
    assert [step.index for step in diagram.steps] == list(range(1, len(diagram.steps) + 1))
    assert all(step.source in lifeline and step.target in lifeline for step in diagram.steps)


def test_sequence_orders_calls_by_source_line(corpus: Corpus) -> None:
    root = corpus.function("PaymentService.applyPayment")
    diagram = algorithms.sequence(corpus.store, root.id, max_depth=1)
    assert diagram is not None
    top_level = [step for step in diagram.steps if step.depth == 1 and step.kind == "call"]
    lines = [step.line for step in top_level if step.line is not None]
    assert lines == sorted(lines)
    assert [step.label for step in top_level][:2] == ["findById()", "existsByReference()"]


def test_sequence_step_limit_sets_truncated(corpus: Corpus) -> None:
    root = corpus.function("InvoiceController.pay")
    diagram = algorithms.sequence(corpus.store, root.id, max_steps=3)
    assert diagram is not None
    assert len(diagram.steps) <= 3 and diagram.truncated


# ------------------------------------------------------------------------------ lineage


def test_lineage_layers_from_entry_point_to_side_effect(corpus: Corpus) -> None:
    root = corpus.function("PaymentService.applyPayment")
    result = algorithms.lineage(corpus.store, root.id)
    assert result is not None
    by_name = {node.qualified_name: result.layers[node.id] for node in result.graph.nodes.values()}
    assert by_name["InvoiceController.pay"] == "entry"
    assert by_name["PaymentService.applyPayment"] == "logic"
    assert by_name["tbl_ledger"] == "table"
    # Whatever only happens because a trigger fired is a side effect, tables included.
    assert by_name["fn_ledger_audit"] == "side_effect"
    assert by_name["tbl_ledger_audit"] == "side_effect"
    assert "query" in by_name.values()
    assert set(result.layers) == set(result.graph.nodes)


def test_lineage_of_a_table_shows_who_reaches_it(corpus: Corpus) -> None:
    result = algorithms.lineage(corpus.store, corpus.table("tbl_ledger_audit").id)
    assert result is not None
    reached = {node.qualified_name for node in result.graph.nodes.values()}
    assert {"fn_ledger_audit", "tbl_ledger"} <= reached
    assert any(edge.type is RelType.FIRES_TRIGGER for edge in result.graph.edges.values())


# ----------------------------------------------------------------------------- topology


def test_topology_groups_directories_and_tables(corpus: Corpus) -> None:
    result = algorithms.topology(corpus.store, corpus.repository_id)
    kinds = {group.kind for group in result.groups}
    assert kinds == {"directory", "table"}
    ids = {group.id for group in result.groups}
    assert all(link.source in ids and link.target in ids for link in result.links)
    assert {link.kind for link in result.links} >= {"calls", "reads", "writes"}
    assert all(link.source != link.target for link in result.links)
    total_functions = sum(group.functions for group in result.groups if group.kind == "directory")
    assert total_functions == corpus.result.stats["functions"]


def test_topology_depth_controls_granularity(corpus: Corpus) -> None:
    coarse = algorithms.topology(corpus.store, corpus.repository_id, depth=1)
    fine = algorithms.topology(corpus.store, corpus.repository_id, depth=6)
    directories = [group.name for group in coarse.groups if group.kind == "directory"]
    assert sorted(directories) == ["db", "scripts", "src"]
    assert len(fine.groups) > len(coarse.groups)
    assert coarse.depth == 1


def test_topology_folds_the_long_tail_into_one_group(corpus: Corpus) -> None:
    full = algorithms.topology(corpus.store, corpus.repository_id, depth=6, max_groups=100)
    capped = algorithms.topology(corpus.store, corpus.repository_id, depth=6, max_groups=5)
    directories = [group for group in capped.groups if group.kind == "directory"]
    assert len(directories) == 5
    assert algorithms.OTHER_GROUP in {group.name for group in directories}

    def total(result: algorithms.Topology, field: str) -> int:
        return sum(getattr(group, field) for group in result.groups if group.kind == "directory")

    # Folding loses no code: totals are unchanged.
    assert total(capped, "files") == total(full, "files")
    assert total(capped, "functions") == total(full, "functions")
    assert total(capped, "lines") == total(full, "lines")


# ------------------------------------------------------------------------- node context


def test_node_context_of_a_function(corpus: Corpus) -> None:
    context = algorithms.node_context(corpus.store, corpus.function("PaymentService.applyPayment").id)
    assert context is not None
    assert [node.qualified_name for node in context.containers if node.label is NodeLabel.CLASS] == [
        "PaymentService"
    ]
    assert [item.node.qualified_name for item in context.callers] == ["InvoiceController.pay"]
    callees = {item.node.qualified_name for item in context.callees}
    assert {
        "FeeCalculator.lateFee",
        "LedgerDao.existsByReference",
        "PaymentService.postThroughProcedure",
    } <= callees
    dates = [item.node.get("authored_at") for item in context.commits]
    assert dates == sorted(dates, reverse=True) and len(dates) >= 3
    assert context.summary()["callers"] == 1


def test_node_context_of_a_table_lists_readers_writers_and_triggers(corpus: Corpus) -> None:
    context = algorithms.node_context(corpus.store, corpus.table("tbl_ledger").id)
    assert context is not None
    writers = {item.node.qualified_name for item in context.writers}
    assert {"LedgerDao.insertEntry", "sp_apply_payment"} <= writers
    assert {item.node.qualified_name for item in context.triggers} == {"trg_ledger_audit"}
    assert context.readers


def test_node_context_of_a_class_lists_members_and_supertypes(corpus: Corpus) -> None:
    context = algorithms.node_context(corpus.store, corpus.node("FeeCalculator", NodeLabel.CLASS).id)
    assert context is not None
    assert {"lateFee", "round"} <= {node.name for node in context.members}


def test_dead_candidate_is_flagged_with_no_callers(corpus: Corpus) -> None:
    dead = corpus.function("Tax2013Adjuster.adjust")
    assert dead.get("dead_candidate") is True
    context = algorithms.node_context(corpus.store, dead.id)
    assert context is not None and context.callers == []


# ------------------------------------------------------------------------------ locator


def test_parse_location_accepts_ranges_and_rejects_prose() -> None:
    assert locator.parse_location("FeeCalculator.java:35") == ("FeeCalculator.java", 35, None)
    assert locator.parse_location("src/a/b.py:10-20") == ("src/a/b.py", 10, 20)
    assert locator.parse_location("see above") is None
    assert locator.parse_location("12:30") is None


def test_resolve_location_by_basename_and_by_path(corpus: Corpus) -> None:
    by_name = locator.resolve_location(corpus.store, corpus.repository_id, "FeeCalculator.java", 36)
    assert [place.scope.qualified_name for place in by_name if place.scope] == ["FeeCalculator.round"]
    full = "src/main/java/com/acme/billing/service/FeeCalculator.java"
    by_path = locator.resolve_location(corpus.store, corpus.repository_id, full, 26)
    assert by_path[0].path == full and by_path[0].scope is not None
    assert by_path[0].scope.qualified_name == "FeeCalculator.lateFee"


def test_resolve_location_rejects_lines_past_the_end_and_unknown_files(corpus: Corpus) -> None:
    assert locator.resolve_location(corpus.store, corpus.repository_id, "FeeCalculator.java", 9999) == []
    assert locator.resolve_location(corpus.store, corpus.repository_id, "Missing.java", 1) == []


def test_find_symbols_by_qualified_name_bare_name_and_cpp_style(corpus: Corpus) -> None:
    def found(text: str) -> list[str]:
        return [
            node.qualified_name for node in locator.find_symbols(corpus.store, corpus.repository_id, text)
        ]

    assert found("PaymentService.applyPayment") == ["PaymentService.applyPayment"]
    assert found("`applyPayment()`") == ["PaymentService.applyPayment"]
    assert found("PaymentService::applyPayment") == ["PaymentService.applyPayment"]
    assert found("tbl_ledger") == ["tbl_ledger"]
    assert found("NoSuchThingAnywhere") == []
