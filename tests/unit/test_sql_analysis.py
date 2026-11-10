"""Table-reference extraction from embedded, dialect-specific and procedural SQL."""

from __future__ import annotations

import pytest

from parsers.sql_analysis import analyze_sql, looks_like_sql, normalize_table_name

# (sql, operation, reads, writes)
STATEMENTS: list[tuple[str, str, set[str], set[str]]] = [
    # --- plain DML -------------------------------------------------------------
    ("SELECT * FROM tbl_acct_v1", "SELECT", {"tbl_acct_v1"}, set()),
    ("select id, name from Accounts where id = ?", "SELECT", {"accounts"}, set()),
    ("SELECT a.id FROM tbl_acct_v1 a WHERE a.flags & ? = ?", "SELECT", {"tbl_acct_v1"}, set()),
    ("SELECT * FROM a, b, c WHERE a.id = b.id", "SELECT", {"a", "b", "c"}, set()),
    ("SELECT * FROM a AS x, b AS y", "SELECT", {"a", "b"}, set()),
    (
        "SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id LEFT OUTER JOIN regions r ON r.id = c.region_id",
        "SELECT",
        {"orders", "customers", "regions"},
        set(),
    ),
    ("SELECT * FROM a NATURAL JOIN b CROSS JOIN c", "SELECT", {"a", "b", "c"}, set()),
    ("SELECT * FROM a JOIN b USING (id)", "SELECT", {"a", "b"}, set()),
    ("INSERT INTO tbl_ledger_audit (acct_id, delta) VALUES (?, ?)", "INSERT", set(), {"tbl_ledger_audit"}),
    ("INSERT INTO archive SELECT * FROM live WHERE ts < ?", "INSERT", {"live"}, {"archive"}),
    ("UPDATE tbl_acct_v1 SET balance = balance - ? WHERE id = ?", "UPDATE", set(), {"tbl_acct_v1"}),
    ("UPDATE accounts a SET a.balance = 0 WHERE a.id = :id", "UPDATE", set(), {"accounts"}),
    ("DELETE FROM sessions WHERE expires < now()", "DELETE", set(), {"sessions"}),
    ("TRUNCATE TABLE staging_a, staging_b", "TRUNCATE", set(), {"staging_a", "staging_b"}),
    ("TRUNCATE staging", "TRUNCATE", set(), {"staging"}),
    # --- sub-queries, set operations, CTEs -------------------------------------------
    ("SELECT * FROM (SELECT id FROM inner_t) t WHERE t.id > 1", "SELECT", {"inner_t"}, set()),
    (
        "SELECT * FROM a WHERE id IN (SELECT a_id FROM b WHERE x IN (SELECT y FROM c))",
        "SELECT",
        {"a", "b", "c"},
        set(),
    ),
    ("SELECT id FROM a UNION ALL SELECT id FROM b EXCEPT SELECT id FROM c", "SELECT", {"a", "b", "c"}, set()),
    ("(SELECT id FROM a) UNION (SELECT id FROM b)", "SELECT", {"a", "b"}, set()),
    ("SELECT (SELECT max(x) FROM m) AS mx, t.* FROM t", "SELECT", {"m", "t"}, set()),
    ("SELECT * FROM a WHERE EXISTS (SELECT 1 FROM b WHERE b.a_id = a.id), c", "SELECT", {"a", "b"}, set()),
    (
        "WITH recent AS (SELECT * FROM orders WHERE ts > ?), big AS (SELECT * FROM recent WHERE total > 100) "
        "SELECT * FROM big b JOIN customers c ON c.id = b.customer_id",
        "SELECT",
        {"orders", "customers"},
        set(),
    ),
    (
        "WITH RECURSIVE tree(id) AS (SELECT id FROM nodes UNION ALL SELECT n.id FROM nodes n JOIN tree t ON n.parent = t.id) SELECT * FROM tree",
        "SELECT",
        {"nodes"},
        set(),
    ),
    (
        "WITH doomed AS (SELECT id FROM stale) DELETE FROM items WHERE id IN (SELECT id FROM doomed)",
        "DELETE",
        {"stale"},
        {"items"},
    ),
    ("WITH src AS (SELECT * FROM raw) INSERT INTO clean SELECT * FROM src", "INSERT", {"raw"}, {"clean"}),
    # --- FROM that is not a table list -----------------------------------------------
    ("SELECT EXTRACT(YEAR FROM created_at) FROM events", "SELECT", {"events"}, set()),
    (
        "SELECT SUBSTRING(name FROM 1 FOR 3), TRIM(BOTH ' ' FROM code) FROM products",
        "SELECT",
        {"products"},
        set(),
    ),
    ("SELECT * FROM t WHERE a IS DISTINCT FROM b AND c IS NOT DISTINCT FROM d", "SELECT", {"t"}, set()),
    ("SELECT DISTINCT a FROM t", "SELECT", {"t"}, set()),
    ("SELECT 'copied FROM legacy_table' AS note FROM real_table", "SELECT", {"real_table"}, set()),
    (
        "SELECT * FROM real_table -- not FROM commented_table\n WHERE x = 1 /* nor FROM this_one */",
        "SELECT",
        {"real_table"},
        set(),
    ),
    ("SELECT * FROM generate_series(1, 10) g JOIN t ON t.n = g", "SELECT", {"t"}, set()),
    ("SELECT * FROM dual", "UNKNOWN", set(), set()),
    # --- keywords that look like DML but are not -------------------------------------
    ("SELECT * FROM tbl_acct_v1 WHERE id = ? FOR UPDATE", "SELECT", {"tbl_acct_v1"}, set()),
    ("SELECT * FROM q FOR UPDATE SKIP LOCKED", "SELECT", {"q"}, set()),
    ("SELECT * FROM q FOR UPDATE OF q NOWAIT", "SELECT", {"q"}, set()),
    ("SELECT * FROM q WHERE id = ? FOR UPDATE WAIT 5", "SELECT", {"q"}, set()),
    ("SELECT * FROM q FOR SHARE", "SELECT", {"q"}, set()),
    ("INSERT INTO kv (k, v) VALUES (?, ?) ON DUPLICATE KEY UPDATE v = VALUES(v)", "INSERT", set(), {"kv"}),
    (
        "INSERT INTO kv (k, v) VALUES ($1, $2) ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v",
        "INSERT",
        set(),
        {"kv"},
    ),
    ("INSERT INTO kv (k) VALUES (?) ON CONFLICT DO NOTHING RETURNING id", "INSERT", set(), {"kv"}),
    # RETURNING ... INTO assigns a PL/SQL variable; it is not a second insert target.
    (
        "INSERT INTO tbl_period (closed_on) SELECT CURRENT_DATE FROM tbl_ledger RETURNING id INTO v_period",
        "INSERT",
        {"tbl_ledger"},
        {"tbl_period"},
    ),
    (
        "INSERT FIRST WHEN x > 1 THEN INTO big (x) VALUES (x) ELSE INTO small (x) VALUES (x) SELECT x FROM src",
        "INSERT",
        {"src"},
        {"big", "small"},
    ),
    ("UPDATE t SET name = REPLACE(name, 'a', 'b')", "UPDATE", set(), {"t"}),
    ("SELECT REPLACE(name, 'x', 'y') FROM t", "SELECT", {"t"}, set()),
    # --- dialects ---------------------------------------------------------------------
    (
        "SELECT TOP 10 * FROM [dbo].[Orders] o WITH (NOLOCK) INNER JOIN [dbo].[Customers] c ON c.Id = o.CustomerId",
        "SELECT",
        {"orders", "customers"},
        set(),
    ),
    ("SELECT * FROM Orders (NOLOCK) WHERE Id = @id", "SELECT", {"orders"}, set()),
    ("SELECT * FROM `my_db`.`user_accounts` WHERE `id` = %s", "SELECT", {"user_accounts"}, set()),
    ('SELECT * FROM "Billing"."Invoice" WHERE "Id" = $1', "SELECT", {"invoice"}, set()),
    (
        "SELECT * FROM billing.invoice i JOIN billing . line_item li ON li.invoice_id = i.id",
        "SELECT",
        {"invoice", "line_item"},
        set(),
    ),
    ("SELECT * FROM ONLY parent_table", "SELECT", {"parent_table"}, set()),
    ("REPLACE INTO cache (k, v) VALUES (?, ?)", "INSERT", set(), {"cache"}),
    ("INSERT OR REPLACE INTO cache (k, v) VALUES (?, ?)", "INSERT", set(), {"cache"}),
    ("INSERT IGNORE INTO seen (id) VALUES (%(id)s)", "INSERT", set(), {"seen"}),
    (
        "INSERT OVERWRITE TABLE warehouse.daily SELECT * FROM staging.daily_raw",
        "INSERT",
        {"daily_raw"},
        {"daily"},
    ),
    (
        "INSERT ALL INTO t_a (x) VALUES (1) INTO t_b (x) VALUES (2) SELECT * FROM dual",
        "INSERT",
        set(),
        {"t_a", "t_b"},
    ),
    (
        "UPDATE o SET o.status = 'X' FROM orders o JOIN flagged f ON f.order_id = o.id",
        "UPDATE",
        {"orders", "flagged"},
        {"orders"},
    ),
    ("UPDATE t SET a = s.a FROM src s WHERE s.id = t.id", "UPDATE", {"src"}, {"t"}),
    ("UPDATE ONLY t SET a = 1", "UPDATE", set(), {"t"}),
    ("DELETE t1 FROM t1 JOIN t2 ON t1.id = t2.id WHERE t2.dead = 1", "DELETE", {"t1", "t2"}, {"t1"}),
    (
        "DELETE FROM films USING producers WHERE producer_id = producers.id",
        "DELETE",
        {"producers"},
        {"films"},
    ),
    (
        "MERGE INTO target t USING source s ON (t.id = s.id) WHEN MATCHED THEN UPDATE SET t.v = s.v "
        "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (s.id, s.v)",
        "MERGE",
        {"source"},
        {"target"},
    ),
    (
        "MERGE INTO tgt USING (SELECT * FROM stg WHERE ok = 1) s ON (tgt.id = s.id) WHEN MATCHED THEN DELETE",
        "MERGE",
        {"stg"},
        {"tgt"},
    ),
    ("LOCK TABLE tbl_ledger_audit IN EXCLUSIVE MODE", "LOCK", {"tbl_ledger_audit"}, set()),
    # --- placeholders and runtime-computed names --------------------------------------
    ("SELECT * FROM ${table} WHERE id = ?", "SELECT", set(), set()),
    ("SELECT * FROM %s WHERE id = %s", "SELECT", set(), set()),
    ("SELECT * FROM {} t JOIN fixed_table f ON f.id = t.id", "SELECT", {"fixed_table"}, set()),
    ("INSERT INTO ${schema}.audit_log (msg) VALUES (#{msg})", "INSERT", set(), set()),
    ("DELETE FROM :tbl WHERE id = :id", "DELETE", set(), set()),
    (
        "SELECT * FROM accounts WHERE id = :id AND owner = @owner AND kind = #{kind} AND x = $1",
        "SELECT",
        {"accounts"},
        set(),
    ),
    ("SELECT * FROM t WHERE name LIKE '%${q}%' AND ts::date = ?", "SELECT", {"t"}, set()),
    # --- DDL with embedded keywords -----------------------------------------------------
    (
        "CREATE TABLE child (id INT PRIMARY KEY, parent_id INT REFERENCES parent(id) ON DELETE CASCADE ON UPDATE RESTRICT, "
        "ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP)",
        "DDL",
        set(),
        set(),
    ),
    (
        "CREATE VIEW v_open AS SELECT o.* FROM orders o JOIN customers c ON c.id = o.cid",
        "DDL",
        {"orders", "customers"},
        set(),
    ),
    ("CREATE TABLE snapshot AS SELECT * FROM live", "DDL", {"live"}, set()),
    ("GRANT SELECT, INSERT, UPDATE, DELETE ON accounts TO app_user", "SELECT", set(), set()),
    ("UPDATE STATISTICS dbo.Orders", "UPDATE", set(), set()),
]


@pytest.mark.parametrize(
    ("sql", "operation", "reads", "writes"), STATEMENTS, ids=[s[0][:60] for s in STATEMENTS]
)
def test_table_extraction(sql: str, operation: str, reads: set[str], writes: set[str]) -> None:
    analysis = analyze_sql(sql)
    assert set(analysis.reads) == reads
    assert set(analysis.writes) == writes
    if reads or writes or operation == "DDL":
        assert analysis.operation == operation


def test_write_ops_record_each_operation_in_order() -> None:
    analysis = analyze_sql("DELETE FROM a WHERE x = 1; INSERT INTO a (x) VALUES (1); UPDATE b SET y = 2")
    assert analysis.write_ops == (("a", "DELETE"), ("a", "INSERT"), ("b", "UPDATE"))
    assert analysis.writes == ("a", "b")
    assert analysis.operation == "DELETE"
    assert analysis.tables == ("a", "b")


def test_aliases_and_ctes_are_scoped_to_one_statement() -> None:
    analysis = analyze_sql(
        "WITH a AS (SELECT * FROM src) SELECT * FROM a; SELECT * FROM a; UPDATE s SET x = 1"
    )
    # in the second statement ``a`` is a real table again
    assert set(analysis.reads) == {"src", "a"}
    analysis = analyze_sql("SELECT * FROM src s; UPDATE s SET x = 1")
    assert analysis.writes == ("s",)


ROUTINES: list[tuple[str, set[str]]] = [
    ("CALL sp_recalc_balances(?, ?)", {"sp_recalc_balances"}),
    ("{call sp_recalc_balances(?, ?)}", {"sp_recalc_balances"}),
    ("{? = call billing.fn_fee(?)}", {"fn_fee"}),
    ("EXEC dbo.usp_NightlyRollup @date = ?", {"usp_nightlyrollup"}),
    ("EXEC @rc = [dbo].[usp_Check] @id", {"usp_check"}),
    ("EXECUTE PROCEDURE audit_row()", {"audit_row"}),
    ("PERFORM refresh_totals(v_id);", {"refresh_totals"}),
    ("CREATE TRIGGER trg AFTER INSERT OR UPDATE ON t FOR EACH ROW EXECUTE FUNCTION fn_audit()", {"fn_audit"}),
]


@pytest.mark.parametrize(("sql", "routines"), ROUTINES, ids=[r[0][:50] for r in ROUTINES])
def test_routine_calls(sql: str, routines: set[str]) -> None:
    analysis = analyze_sql(sql)
    assert set(analysis.routines) == routines
    assert not analysis.writes


def test_function_style_invocations_are_reported_separately() -> None:
    analysis = analyze_sql("SELECT fn_fee(amount), COUNT(*) FROM charges WHERE ts > now()")
    assert analysis.reads == ("charges",)
    assert {"fn_fee", "count", "now"} <= set(analysis.functions)
    assert analysis.routines == ()
    oracle_block = analyze_sql("BEGIN pkg_recon.run_nightly(?, ?); END;")
    assert "run_nightly" in oracle_block.functions


def test_dynamic_sql_inside_execute_is_analysed() -> None:
    plpgsql = analyze_sql("EXECUTE 'INSERT INTO audit_log (msg) VALUES ($1)' USING v_msg;")
    assert plpgsql.writes == ("audit_log",)
    assert plpgsql.routines == ()
    tsql = analyze_sql("EXEC sp_executesql N'DELETE FROM tmp_rows WHERE batch = @b', N'@b int', @b = 1")
    assert tsql.writes == ("tmp_rows",)
    assert tsql.routines == ()
    formatted = analyze_sql("EXECUTE format('UPDATE counters SET n = n + 1 WHERE it''s = %L', v);")
    assert formatted.writes == ("counters",)
    oracle = analyze_sql("EXECUTE IMMEDIATE 'TRUNCATE TABLE stage_rows';")
    assert oracle.writes == ("stage_rows",)


def test_procedural_body() -> None:
    body = """
    DECLARE v_total NUMERIC; cur CURSOR FOR SELECT id FROM tbl_acct_v1;
    BEGIN
        SELECT SUM(amount) INTO v_total FROM tbl_txn WHERE acct_id = NEW.id;
        IF EXISTS (SELECT 1 FROM tbl_holds h WHERE h.acct_id = NEW.id) THEN
            RAISE NOTICE 'skipping update of % from holds', NEW.id;
            RETURN NEW;
        END IF;
        FOR r IN SELECT * FROM tbl_rates LOOP
            UPDATE tbl_acct_v1 SET balance = v_total * r.rate WHERE id = NEW.id;
        END LOOP;
        FETCH NEXT FROM cur INTO v_id;
        INSERT INTO tbl_ledger_audit (acct_id, delta) VALUES (NEW.id, v_total);
        PERFORM pg_notify('ledger', NEW.id::text);
        DELETE FROM tbl_txn_staging WHERE acct_id = NEW.id;
        RETURN NEW;
    END;
    """
    analysis = analyze_sql(body)
    assert set(analysis.reads) == {"tbl_acct_v1", "tbl_txn", "tbl_holds", "tbl_rates"}
    assert analysis.write_ops == (
        ("tbl_acct_v1", "UPDATE"),
        ("tbl_ledger_audit", "INSERT"),
        ("tbl_txn_staging", "DELETE"),
    )
    assert analysis.routines == ("pg_notify",)


def test_ddl_targets() -> None:
    assert analyze_sql("CREATE TABLE IF NOT EXISTS billing.tbl_acct_v2 (id BIGINT)").ddl == (
        ("CREATE TABLE", "tbl_acct_v2"),
    )
    assert analyze_sql("ALTER TABLE tbl_acct_v1 ADD COLUMN flags INT").ddl == (
        ("ALTER TABLE", "tbl_acct_v1"),
    )
    assert analyze_sql("DROP TABLE IF EXISTS old_stuff CASCADE").ddl == (("DROP TABLE", "old_stuff"),)
    assert analyze_sql(
        "CREATE OR REPLACE FUNCTION f_x() RETURNS trigger AS $$ BEGIN RETURN NEW; END $$"
    ).ddl == (("CREATE FUNCTION", "f_x"),)
    assert analyze_sql("CREATE UNIQUE INDEX CONCURRENTLY idx_a ON accounts (email)").ddl == (
        ("CREATE INDEX", "idx_a"),
    )
    assert analyze_sql("CREATE GLOBAL TEMPORARY TABLE tmp_x (id INT)").ddl == (("CREATE TABLE", "tmp_x"),)


def test_normalize_table_name() -> None:
    assert normalize_table_name("TBL_Acct_V1") == "tbl_acct_v1"
    assert normalize_table_name("[dbo].[Order Details]") is None  # spaces: not representable
    assert normalize_table_name("[dbo].[Orders]") == "orders"
    assert normalize_table_name('"public"."Invoice"') == "invoice"
    assert normalize_table_name("db . schema . `t`") == "t"
    assert normalize_table_name("t.*") is None
    assert normalize_table_name("dual") is None


IS_SQL = [
    "SELECT * FROM tbl_acct_v1 WHERE id = ?",
    "select id from accounts",
    "  \n  SELECT a.id,\n         a.name\n    FROM accounts a\n",
    "/* nightly */ SELECT id FROM accounts",
    "-- header\nSELECT id FROM accounts",
    "(SELECT id FROM a) UNION (SELECT id FROM b)",
    "SELECT COUNT(*) FROM ${table}",
    "SELECT * FROM %s WHERE id = %s",
    "Select * From Users Where Id = @id",
    "SELECT fn_next_batch(?)",
    "SELECT * FROM fn_open_items(?)",
    "INSERT INTO tbl_ledger_audit (a, b) VALUES (?, ?)",
    "insert ignore into seen (id) values (?)",
    "INSERT OR REPLACE INTO cache VALUES (?, ?)",
    "INSERT Orders (Id, Total) VALUES (@id, @total)",
    "REPLACE INTO cache (k, v) VALUES (?, ?)",
    "UPDATE tbl_acct_v1 SET balance = ? WHERE id = ?",
    "UPDATE accounts a SET a.balance = 0",
    "UPDATE ${table} SET x = 1",
    "UPDATE t SET (a, b) = (SELECT 1, 2)",
    "DELETE FROM sessions WHERE expires < ?",
    "DELETE FROM sessions",
    "DELETE Account a WHERE a.closed = true",
    "MERGE INTO t USING s ON (t.id = s.id) WHEN MATCHED THEN DELETE",
    "WITH x AS (SELECT 1 FROM t) SELECT * FROM x",
    "WITH RECURSIVE tree(id) AS (SELECT 1) SELECT * FROM tree",
    "CALL sp_recalc_balances(?, ?)",
    "{call sp_recalc_balances(?, ?)}",
    "{? = call fn_fee(?)}",
    "EXEC dbo.usp_NightlyRollup @date = ?",
    "EXEC usp_NightlyRollup",
    "TRUNCATE TABLE staging",
    "TRUNCATE staging;",
    "CREATE TABLE IF NOT EXISTS t (id INT)",
    "ALTER TABLE t ADD COLUMN c INT",
    "DROP INDEX idx_a",
    "LOCK TABLE tbl_ledger_audit IN EXCLUSIVE MODE",
    "from Account a where a.status = :status",
    "FROM Account WHERE id = ?",
    "BEGIN pkg_recon.run_nightly(?, ?); END;",
]

NOT_SQL = [
    "",
    "select",
    "SELECT",
    "Select a file from the list",
    "Select an option",
    "Select option from menu",
    "Select items from the list.",
    "Selected rows from the table below",
    "Update failed",
    "Update available: restart to install",
    "Update profile set to public",
    "Delete from cart",
    "Delete this item from your list?",
    "Insert into document",
    "Insert coin to continue",
    "Create table of contents",
    "Drop files here to upload",
    "Call me maybe (later)",
    "Execute order now",
    "Truncate text to 80 characters",
    "truncate output",
    "With great power comes great responsibility",
    "From Monday where possible, use the side entrance",
    "from now on",
    "Merge branch 'main' into feature",
    "Begin transaction log",
    "application/json; charset=utf-8",
    "https://example.com/select?from=users",
    "user.selected.from.list",
    "The update of accounts set a record",
]


@pytest.mark.parametrize("text", IS_SQL)
def test_looks_like_sql_accepts(text: str) -> None:
    assert looks_like_sql(text)


@pytest.mark.parametrize("text", NOT_SQL)
def test_looks_like_sql_rejects(text: str) -> None:
    assert not looks_like_sql(text)


def test_detector_is_linear_on_adversarial_input() -> None:
    import time

    hostile = "SELECT " + "a, " * 40_000 + "b"
    started = time.perf_counter()
    assert not looks_like_sql(hostile)
    analyze_sql(hostile)
    hostile_insert = "INSERT t (" + "x, " * 40_000
    assert not looks_like_sql(hostile_insert)
    assert time.perf_counter() - started < 2.0


# ------------------------------------------------------------ cross-check with a grammar

STANDARD_STATEMENTS = [
    "SELECT a.id, b.name FROM accounts a JOIN branches b ON b.id = a.branch_id WHERE a.balance > 10",
    "SELECT * FROM orders WHERE customer_id IN (SELECT id FROM customers WHERE region = 'EU')",
    "SELECT o.id FROM orders o LEFT JOIN items i ON i.order_id = o.id LEFT JOIN products p ON p.id = i.product_id",
    "SELECT id FROM a UNION SELECT id FROM b",
    "INSERT INTO audit_log (id, msg) VALUES (1, 'x')",
    "INSERT INTO archive SELECT * FROM live WHERE ts < 10",
    "UPDATE accounts SET balance = 0 WHERE id = 1",
    "DELETE FROM sessions WHERE expires < 10",
    "WITH recent AS (SELECT * FROM orders) SELECT * FROM recent JOIN customers c ON c.id = recent.cid",
    "SELECT count(*) FROM ledger GROUP BY acct HAVING count(*) > 1 ORDER BY 1",
]


def _grammar_tables(sql: str) -> tuple[set[str], set[str]] | None:
    """Tables referenced by ``sql`` according to the Tree-sitter SQL grammar: ``(reads, writes)``."""
    import tree_sitter_sql
    from tree_sitter import Language, Parser

    root = Parser(Language(tree_sitter_sql.language())).parse(sql.encode()).root_node
    if root.has_error:
        return None
    reads: set[str] = set()
    writes: set[str] = set()
    cte_names: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "cte":
            name = next((c for c in node.named_children if c.type == "identifier"), None)
            if name is not None and name.text:
                cte_names.add(name.text.decode().lower())
        if node.type == "relation":
            reference = next((c for c in node.named_children if c.type == "object_reference"), None)
            if reference is not None:
                name = reference.child_by_field_name("name")
                if name is not None and name.text:
                    parent = node.parent.type if node.parent is not None else ""
                    (writes if parent == "update" else reads).add(name.text.decode().lower())
        elif node.type in ("insert", "delete"):
            holder = node if node.type == "insert" else node.next_named_sibling
            reference = next(
                (c for c in (holder.named_children if holder else []) if c.type == "object_reference"), None
            )
            if reference is not None:
                name = reference.child_by_field_name("name")
                if name is not None and name.text:
                    writes.add(name.text.decode().lower())
        stack.extend(node.named_children)
    return reads - cte_names - writes, writes


@pytest.mark.parametrize("sql", STANDARD_STATEMENTS)
def test_agrees_with_tree_sitter_grammar_on_standard_sql(sql: str) -> None:
    expected = _grammar_tables(sql)
    assert expected is not None, "the grammar is expected to accept this statement"
    assert expected[0] or expected[1], "the grammar-based oracle found no table: the check would be vacuous"
    analysis = analyze_sql(sql)
    assert set(analysis.reads) - set(analysis.writes) == expected[0]
    assert set(analysis.writes) == expected[1]
