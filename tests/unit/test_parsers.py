"""Semantic-scope extraction for every supported language."""

from __future__ import annotations

import textwrap

import pytest

from parsers.ir import ClassKind, FunctionDef, FunctionKind, Language, ModuleDef
from parsers.registry import SUPPORTED_EXTENSIONS, language_for_path, parse_file
from parsers.sql import split_statements


def parse(path: str, source: str) -> ModuleDef:
    return parse_file(path, textwrap.dedent(source).lstrip("\n").encode())


def fn(module: ModuleDef, qualified_name: str) -> FunctionDef:
    matches = [f for f in module.all_functions() if f.qualified_name == qualified_name]
    assert len(matches) == 1, f"{qualified_name}: {[f.qualified_name for f in module.all_functions()]}"
    return matches[0]


def calls(function: FunctionDef) -> set[tuple[str | None, str, str]]:
    return {(c.receiver, c.name, c.kind) for c in function.calls}


def test_language_detection() -> None:
    assert language_for_path("src/A.java") is Language.JAVA
    assert language_for_path("a/b/app.tsx") is Language.TYPESCRIPT
    assert language_for_path("legacy/ledger.hpp") is Language.CPP
    assert language_for_path("db/V1__init.SQL") is Language.SQL
    assert language_for_path("types/index.d.ts") is None
    assert language_for_path("README.md") is None
    assert ".py" in SUPPORTED_EXTENSIONS
    with pytest.raises(ValueError, match="unsupported"):
        parse_file("notes.txt", b"")


# ----------------------------------------------------------------------------- Java

JAVA = """
    package com.acme.billing.recon;

    import java.util.List;
    import static com.acme.billing.Queries.FIND_ALL;
    import com.acme.billing.dao.*;

    /** Reconciles ledgers every night. */
    @Component
    @RequestMapping("/api/recon")
    public class ReconciliationWorker extends BaseWorker implements Runnable, Auditable {
        private static final String TABLE = "tbl_acct_v1";
        private static final String SQL_FLAGGED = "SELECT a.id " +
            "FROM " + TABLE + " a WHERE a.flags & ? <> 0";
        private final LegacyAccountDAO dao;

        public ReconciliationWorker(LegacyAccountDAO dao) { super("recon"); this.dao = dao; }

        // HACK: keep the lock order (ticket OPS-4411).
        @Scheduled(cron = "0 0 2 * * *")
        public void processNightlyReconciliation(int batch, final String region) throws Exception {
            List<Account> accounts = dao.findFlagged(batch);
            var loader = new FXRatesLoader();
            accounts.forEach(a -> this.audit(a));
            accounts.stream().map(Account::getId).forEach(this::audit);
            jdbc.query(SQL_FLAGGED, batch);
            StringBuilder sb = new StringBuilder("UPDATE tbl_acct_v1 SET balance = 0");
            sb.append(" WHERE region = '").append(region).append("'");
            if (batch > 1) { sb.append(" AND batch = ?"); }
            jdbc.update(sb.toString());
            String dynamic = String.format("DELETE FROM %s WHERE id = ?", region);
            String block = \"\"\"
                INSERT INTO tbl_ledger_audit (acct_id, delta)
                VALUES (?, ?)
                \"\"\";
            log.info("Update failed for batch " + batch);
            jdbc.call(Queries.RECALC);
            Runnable r = new Runnable() { public void run() { helper(); } };
        }

        @GetMapping("/status/{id}")
        public Status status(@PathVariable long id) { return null; }

        @Query("SELECT u FROM User u WHERE u.id = :id")
        abstract User find(long id);

        enum Mode { A, B; void describe() {} }
        interface Callback { String Q = "DELETE FROM tbl_tmp"; void done(); }
    }
"""


def test_java_structure() -> None:
    module = parse("src/main/java/com/acme/billing/recon/ReconciliationWorker.java", JAVA)
    assert module.language is Language.JAVA
    assert module.package == "com.acme.billing.recon"
    assert module.error_count == 0
    assert [(i.module, i.name, i.is_static) for i in module.imports] == [
        ("java.util", "List", False),
        ("com.acme.billing.Queries", "FIND_ALL", True),
        ("com.acme.billing.dao", "*", False),
    ]
    classes = {c.qualified_name: c for c in module.classes}
    assert set(classes) == {
        "ReconciliationWorker",
        "ReconciliationWorker.Mode",
        "ReconciliationWorker.Callback",
    }
    worker = classes["ReconciliationWorker"]
    assert worker.bases == ["BaseWorker"]
    assert worker.interfaces == ["Runnable", "Auditable"]
    assert worker.docstring == "Reconciles ledgers every night."
    assert worker.fields["dao"] == "LegacyAccountDAO"
    assert worker.constants["SQL_FLAGGED"].value == "SELECT a.id FROM tbl_acct_v1 a WHERE a.flags & ? <> 0"
    assert classes["ReconciliationWorker.Mode"].kind is ClassKind.ENUM
    assert classes["ReconciliationWorker.Callback"].kind is ClassKind.INTERFACE
    assert classes["ReconciliationWorker.Callback"].owner == "ReconciliationWorker"
    assert fn(module, "ReconciliationWorker.Callback.done").is_abstract
    assert fn(module, "ReconciliationWorker.ReconciliationWorker").kind is FunctionKind.CONSTRUCTOR


def test_java_method_body() -> None:
    module = parse("ReconciliationWorker.java", JAVA)
    method = fn(module, "ReconciliationWorker.processNightlyReconciliation")
    assert method.entry_point is not None and method.entry_point.kind == "scheduled"
    assert method.entry_point.detail == "cron=0 0 2 * * *"
    assert method.docstring == "HACK: keep the lock order (ticket OPS-4411)."
    # the extent includes the annotation; the citation anchor is the line of the name
    assert method.start_line == method.name_line - 1
    assert [(p.name, p.type) for p in method.params] == [("batch", "int"), ("region", "String")]
    assert method.local_types["loader"] == "FXRatesLoader"
    assert {
        ("dao", "findFlagged", "call"),
        (None, "FXRatesLoader", "new"),
        ("this", "audit", "call"),          # inside the lambda
        ("Account", "getId", "reference"),  # method reference
        ("this", "audit", "reference"),
        (None, "helper", "call"),           # inside the anonymous class
    } <= calls(method)  # fmt: skip
    by_operation = {s.operation: s for s in method.sql}
    select = by_operation["SELECT"]
    assert select.reads == ("tbl_acct_v1",)
    assert select.used_at is not None and select.line < select.used_at  # defined as a constant, used here
    update = by_operation["UPDATE"]
    assert update.writes == ("tbl_acct_v1",)
    assert update.dynamic  # an appended runtime value and a conditional fragment
    assert update.text == "UPDATE tbl_acct_v1 SET balance = 0 WHERE region = '${region}' AND batch = ?"
    assert by_operation["DELETE"].dynamic and by_operation["DELETE"].writes == ()  # table name is a parameter
    assert by_operation["INSERT"].writes == ("tbl_ledger_audit",)
    assert by_operation["INSERT"].text.startswith("INSERT INTO tbl_ledger_audit")
    assert len(method.sql) == 4, "a log message that starts with a SQL verb is not a query"
    assert [(r.qualifier, r.name) for r in method.constant_refs] == [("Queries", "RECALC")]


def test_java_annotations() -> None:
    module = parse("ReconciliationWorker.java", JAVA)
    status = fn(module, "ReconciliationWorker.status")
    assert status.entry_point is not None
    assert (status.entry_point.kind, status.entry_point.method, status.entry_point.route) == (
        "http",
        "GET",
        "/api/recon/status/{id}",
    )
    find = fn(module, "ReconciliationWorker.find")
    assert find.is_abstract
    assert [s.reads for s in find.sql] == [("user",)]


def test_java_orm_table_and_main() -> None:
    module = parse(
        "Account.java",
        """
        @Entity
        @Table(name = "tbl_acct_v1")
        public class Account {
            public static void main(String[] args) { new Account().run(); }
        }
        """,
    )
    assert module.classes[0].table == "tbl_acct_v1"
    main = fn(module, "Account.main")
    assert main.entry_point is not None and main.entry_point.kind == "main"
    assert ("type:Account", "run", "call") in calls(main)


def test_deep_concatenation_does_not_overflow_the_stack() -> None:
    pieces = " + ".join(f'"col{i}, "' for i in range(3000))
    source = f'class Big {{ void m() {{ String q = "SELECT " + {pieces} + "1 FROM tbl_wide"; }} }}'
    module = parse_file("Big.java", source.encode())
    assert [s.reads for s in fn(module, "Big.m").sql] == [("tbl_wide",)]


# --------------------------------------------------------------------------- Python

PYTHON = '''
    """Audit listener."""
    import os, sys as system
    from .dao import LegacyDao as Dao
    from ..pkg.mod import *

    TABLE = "tbl_audit"
    SQL_ALL = "SELECT * FROM tbl_a"
    SQL_DELETE = f"DELETE FROM {TABLE} WHERE id = %s"

    app = Flask(__name__)


    class AuditLogListener(BaseListener):
        """Listens for ledger events."""

        QUERY = """
            INSERT INTO tbl_ledger_audit (a) VALUES (%s)
        """
        __tablename__ = "tbl_listener_state"

        def __init__(self, conn: Connection, dao: Dao = None):
            super().__init__(conn)
            self.dao = dao
            self.helper = Helper(conn)

        @app.route("/audit/<int:id>", methods=["POST", "PUT"])
        async def on_event(self, event: dict, *args, **kwargs) -> None:
            cur = self.conn.cursor()
            cur.execute(self.QUERY, (event["id"],))
            cur.execute(SQL_ALL)
            cur.execute("UPDATE tbl_x SET a = %s WHERE id = %s" % (1, 2))
            q = f"SELECT * FROM {TABLE} t JOIN tbl_y y ON y.id = t.id WHERE t.v = {event['v']}"
            q += " ORDER BY 1"
            cur.execute(q)
            parts = ["SELECT id", "FROM tbl_parts"]
            parts.append("WHERE x = 1")
            cur.execute(" ".join(parts))
            worker = Thread(target=self.flush)
            self.helper.run(lambda x: self.dao.save(x))

            def inner(y):
                return Dao.static_thing(y)

            try:
                inner(1)
            except ValueError as exc:
                log.error("Update failed for %s", exc)

        def flush(self): ...


    @celery.task(bind=True)
    def nightly(self):
        process()


    if __name__ == "__main__":
        nightly.delay()
'''


def test_python_structure() -> None:
    module = parse("billing/listeners/audit.py", PYTHON)
    assert module.docstring == "Audit listener."
    assert [(i.module, i.name, i.alias, i.level) for i in module.imports] == [
        ("os", None, "os", 0),
        ("sys", None, "system", 0),
        ("dao", "LegacyDao", "Dao", 1),
        ("pkg.mod", "*", None, 2),
    ]
    assert module.constants["SQL_DELETE"].value == "DELETE FROM tbl_audit WHERE id = %s"
    cls = module.classes[0]
    assert cls.name == "AuditLogListener" and cls.bases == ["BaseListener"]
    assert cls.docstring == "Listens for ledger events."
    assert cls.table == "tbl_listener_state"
    assert cls.fields == {"dao": "Dao", "helper": "Helper"}
    init = fn(module, "AuditLogListener.__init__")
    assert init.kind is FunctionKind.CONSTRUCTOR
    assert [(p.name, p.type) for p in init.params] == [("conn", "Connection"), ("dao", "Dao")]
    assert ("super", "__init__", "call") in calls(init)


def test_python_method_body() -> None:
    module = parse("audit.py", PYTHON)
    method = fn(module, "AuditLogListener.on_event")
    assert method.entry_point is not None
    assert (method.entry_point.kind, method.entry_point.method, method.entry_point.route) == (
        "http",
        "POST,PUT",
        "/audit/<int:id>",
    )
    assert "async" in method.modifiers
    assert {
        ("self.conn", "cursor", "call"),
        ("self.dao", "save", "call"),      # inside the lambda
        ("self", "flush", "reference"),    # Thread(target=self.flush)
        (None, "inner", "call"),
    } <= calls(method)  # fmt: skip
    statements = {(s.operation, s.reads, s.writes, s.dynamic) for s in method.sql}
    assert statements == {
        ("INSERT", (), ("tbl_ledger_audit",), False),  # class constant via self.QUERY
        ("SELECT", ("tbl_a",), (), False),  # module constant
        ("UPDATE", (), ("tbl_x",), True),  # % formatting
        ("SELECT", ("tbl_audit", "tbl_y"), (), True),  # f-string with a constant and a runtime value
        ("SELECT", ("tbl_parts",), (), False),  # list of fragments joined later
    }
    nested = fn(module, "AuditLogListener.on_event.inner")
    assert nested.parent_function == "AuditLogListener.on_event"
    assert ("Dao", "static_thing", "call") in calls(nested)


def test_python_module_scope_and_tasks() -> None:
    module = parse("audit.py", PYTHON)
    task = fn(module, "nightly")
    assert task.entry_point is not None and task.entry_point.kind == "task"
    script = fn(module, "<module>")
    assert script.kind is FunctionKind.SCRIPT
    assert script.entry_point is not None and script.entry_point.kind == "main"
    assert ("nightly", "delay", "call") in calls(script)
    assert script.sql == [], "defining a query constant is not running it"


# ----------------------------------------------------------------- JavaScript / TypeScript

JAVASCRIPT = """
    #!/usr/bin/env node
    const express = require('express');
    const { Pool, Client: PgClient } = require('pg');
    const dao = require('./dao/accountDao');
    import defaultThing, { named as alias } from './lib/things.js';
    const catchAsync = require('../utils/catchAsync');

    const FIND_SQL = `SELECT * FROM tbl_orders WHERE id = $1`;
    const router = express.Router();

    // Legacy account model.
    function Account(db) {
      this.db = db;
      this.refresh = function () { return this.load(); };
    }
    util.inherits(Account, Base);
    Account.prototype.load = function (id, cb) {
      this.db.query("SELECT * FROM tbl_legacy WHERE id = " + id, function (err, rows) {
        if (err) return cb(err);
        cb(null, rows);
      });
    };

    class OrderService extends BaseService {
      static TABLE = 'tbl_orders';
      constructor(pool) { super(pool); this.audit = new AuditLog(pool); }
      async find(id) {
        const res = await this.pool.query(FIND_SQL, [id]);
        await this.pool.query(`UPDATE ${OrderService.TABLE} SET seen = now() WHERE id = ${id}`);
        return res.rows.map((r) => dao.clean(r));
      }
    }

    exports.getUser = catchAsync(async (req, res) => { res.json(await dao.find(req.params.id)); });
    const helper = async (x) => { await dao.save(x); };

    module.exports = function configure(app) {
      app.get('/health', (req, res) => { res.send(helper('ok')); });
      app.post('/orders/:id', authenticate, exports.getUser);
      router.route('/x').get(listX).post(createX);
      cron.schedule('0 2 * * *', () => new OrderService(pool).find(1));
      return { stop: function () { clearInterval(timer); } };
    };
"""


def test_javascript_bindings_and_classes() -> None:
    module = parse("src/app.js", JAVASCRIPT)
    assert module.error_count == 0
    assert [(i.module, i.name, i.alias) for i in module.imports] == [
        ("express", None, "express"),
        ("pg", "Pool", "Pool"),
        ("pg", "Client", "PgClient"),
        ("./dao/accountDao", None, "dao"),
        ("./lib/things.js", "default", "defaultThing"),
        ("./lib/things.js", "named", "alias"),
        ("../utils/catchAsync", None, "catchAsync"),
    ]
    classes = {c.name: c for c in module.classes}
    account = classes["Account"]  # prototype-style class
    assert account.bases == ["Base"]
    assert account.docstring == "Legacy account model."
    assert [m.qualified_name for m in account.methods] == [
        "Account.constructor",
        "Account.refresh",
        "Account.load",
    ]
    assert account.methods[0].kind is FunctionKind.CONSTRUCTOR
    load = fn(module, "Account.load")
    assert [s.reads for s in load.sql] == [("tbl_legacy",)] and load.sql[0].dynamic
    assert ("this.db", "query", "call") in calls(load)
    assert (None, "cb", "call") in calls(load), "anonymous callbacks belong to the enclosing function"
    assert not any(c.name in ("err", "rows") for c in load.calls), "callback parameters are not functions"

    service = classes["OrderService"]
    assert service.bases == ["BaseService"]
    assert service.fields == {"audit": "AuditLog"}
    find = fn(module, "OrderService.find")
    assert {(s.operation, s.reads, s.writes) for s in find.sql} == {
        ("SELECT", ("tbl_orders",), ()),
        ("UPDATE", (), ("tbl_orders",)),
    }
    assert module.default_export == "configure"
    assert fn(module, "getUser").name == "getUser", "catchAsync(fn) defines getUser"
    assert ("dao", "save", "call") in calls(fn(module, "helper"))
    assert fn(module, "configure.stop").parent_function == "configure"


def test_javascript_registered_entry_points() -> None:
    module = parse("src/app.js", JAVASCRIPT)
    routes = {f.name: f for f in module.functions if f.kind is FunctionKind.ROUTE}
    assert set(routes) == {"GET /health", "POST /orders/:id", "GET /x", "POST /x", "schedule 0 2 * * *"}
    health = routes["GET /health"]
    assert health.entry_point is not None and health.entry_point.route == "/health"
    assert (None, "helper", "call") in calls(health)
    assert calls(routes["POST /orders/:id"]) == {
        (None, "authenticate", "reference"),
        (None, "getUser", "reference"),
    }
    assert calls(routes["POST /x"]) == {(None, "createX", "reference")}
    scheduled = routes["schedule 0 2 * * *"]
    assert scheduled.entry_point is not None and scheduled.entry_point.kind == "scheduled"
    assert ("type:OrderService", "find", "call") in calls(scheduled)
    configure = fn(module, "configure")
    assert not any(c.name in ("get", "post", "schedule") for c in configure.calls)
    assert fn(module, "<module>").entry_point is not None  # hash-bang line


def test_javascript_module_wrappers_and_object_namespaces() -> None:
    module = parse(
        "legacy/widget.js",
        """
        (function (root, factory) { root.Widget = factory(); }(this, function () {
          function inside() {}
          var View = Backbone.View.extend({
            render: function () { inside(); return this; }
          });
          var api = { list() { return this.fetch('/x'); }, fetch: (url) => http.get(url) };
          return { inside: inside };
        }));
        """,
    )
    names = {f.qualified_name for f in module.all_functions()}
    assert {"inside", "View.render", "api.list", "api.fetch"} <= names
    view = next(c for c in module.classes if c.name == "View")
    assert view.bases == ["View"] or view.bases == ["Backbone.View"] or view.bases
    assert (None, "inside", "call") in calls(fn(module, "View.render"))


TYPESCRIPT = """
    import { Injectable } from '@nestjs/common';
    import type { Order } from './order';
    export * from './barrel';

    export interface Repo<T> extends Base { find(id: string): Promise<T>; }
    export enum Mode { A = 'a' }

    /** Order endpoints. */
    @Controller('orders')
    export class OrderController extends BaseCtl implements Repo<Order> {
      static readonly SQL = 'SELECT 1 FROM tbl_ts';
      constructor(private readonly svc: OrderService, public repo: Repo<Order>, plain: number) { super(); }

      @Get(':id')
      async getOne(@Param('id') id: string): Promise<Order | null> {
        const helper: Helper = new Helper();
        await this.db.query(OrderController.SQL);
        return (await this.repo.find(id)) as Order;
      }
      private total = (a: number): number => a + this.calc(a);
      abstract todo(): void;
    }
    export const handler = async (event: Event) => { await run(event); };
    export default function main() { handler(null); }
"""


def test_typescript() -> None:
    module = parse("src/order.controller.ts", TYPESCRIPT)
    assert module.language is Language.TYPESCRIPT and module.error_count == 0
    assert ("./barrel", "*", None) in [(i.module, i.name, i.alias) for i in module.imports]
    classes = {c.name: c for c in module.classes}
    assert classes["Repo"].kind is ClassKind.INTERFACE and classes["Repo"].bases == ["Base"]
    assert fn(module, "Repo.find").is_abstract
    assert classes["Mode"].kind is ClassKind.ENUM
    controller = classes["OrderController"]
    assert controller.bases == ["BaseCtl"] and controller.interfaces == ["Repo"]
    assert controller.decorators == ["Controller"]
    assert controller.docstring == "Order endpoints."
    assert controller.fields == {"svc": "OrderService", "repo": "Repo"}  # parameter properties only
    get_one = fn(module, "OrderController.getOne")
    assert get_one.entry_point is not None
    assert (get_one.entry_point.method, get_one.entry_point.route) == ("GET", "/orders/:id")
    assert get_one.local_types["helper"] == "Helper"
    assert [s.reads for s in get_one.sql] == [("tbl_ts",)]
    assert ("this.repo", "find", "call") in calls(get_one)
    assert ("this", "calc", "call") in calls(fn(module, "OrderController.total"))
    assert fn(module, "OrderController.todo").is_abstract
    handler = fn(module, "handler")
    assert handler.entry_point is not None and handler.entry_point.kind == "message"
    assert module.default_export == "main"


def test_tsx_components_are_calls() -> None:
    module = parse(
        "ui/App.tsx",
        """
        export function App({ items }: Props) {
          return <Layout><ns.Thing />{items.map((i) => <Row key={i.id} item={load(i)} />)}<div /></Layout>;
        }
        """,
    )
    assert module.error_count == 0
    assert {
        (None, "Layout", "call"),
        ("ns", "Thing", "call"),
        (None, "Row", "call"),
        (None, "load", "call"),
    } <= calls(fn(module, "App"))
    assert not any(c.name == "div" for c in fn(module, "App").calls)


# ------------------------------------------------------------------------------ C++

CPP = """
    #include <string>
    #include "ledger/account.h"
    #define SQL_DEF "SELECT 1 FROM tbl_def"
    namespace billing { namespace core {
    static const char* kFindSql = "SELECT id FROM tbl_acct_v1 "
                                  "WHERE id = ?";
    /// Base processor.
    class Processor : public Base, private Mixin {
     public:
      Processor(Db* db) : db_(db), helper_(new Helper(db)) { init(); }
      virtual int run(const Account& acct, int flags = 0) const = 0;
      static Processor* create(Db* db);
      int inlineMethod() { return helper_->compute(1) + this->run(a_, 0); }
     private:
      Db* db_;
      std::unique_ptr<Helper> helper_;
    };
    int Processor::runAll(std::vector<Account>& accts) {
      std::string sql = "INSERT INTO tbl_ledger_audit (id) VALUES (";
      sql += std::to_string(accts.size());
      sql.append(")");
      std::ostringstream oss;
      oss << "DELETE FROM tbl_tmp WHERE id = " << accts[0].id << " AND x = 1";
      db_->exec(kFindSql);
      db_->exec(oss.str());
      std::cerr << "Delete from cache failed " << 1;
      Helper h(db_);
      h.go();
      auto p = std::make_unique<Worker>(db_);
      p->start();
      EXEC SQL UPDATE tbl_host SET a = :v WHERE id = :id;
      return ::global_fn(1);
    }
    }}
    int main(int argc, char** argv) { billing::core::Processor::create(nullptr)->run(Account(), 1); return 0; }
"""


def test_cpp() -> None:
    module = parse("src/processor.cpp", CPP)
    assert module.package == "billing::core"
    assert [i.module for i in module.imports] == ["ledger/account.h"], "system headers are not project files"
    assert module.constants["SQL_DEF"].value == "SELECT 1 FROM tbl_def"
    cls = module.classes[0]
    assert cls.name == "Processor" and cls.bases == ["Base", "Mixin"]
    assert cls.docstring == "Base processor."
    assert cls.fields == {"db_": "Db", "helper_": "Helper"}
    assert [m.qualified_name for m in cls.methods] == [
        "Processor::Processor",
        "Processor::run",
        "Processor::inlineMethod",
    ]
    assert fn(module, "Processor::run").is_abstract, "pure virtual methods exist only as declarations"
    constructor = fn(module, "Processor::Processor")
    assert constructor.kind is FunctionKind.CONSTRUCTOR
    assert (None, "Helper", "new") in calls(constructor), "member initialisers are part of the constructor"

    run_all = fn(module, "Processor::runAll")  # out-of-line definition: owner known by name only
    assert run_all.owner == "Processor" and run_all.kind is FunctionKind.METHOD
    assert run_all not in cls.methods
    assert run_all.local_types["h"] == "Helper" and run_all.local_types["p"] == "Worker"
    assert {
        ("db_", "exec", "call"),
        ("h", "go", "call"),
        ("p", "start", "call"),
        (None, "global_fn", "call"),
    } <= calls(run_all)
    assert (None, "Worker", "new") in calls(run_all)
    assert not any(c.name == "to_string" for c in run_all.calls)
    assert {(s.operation, s.reads, s.writes) for s in run_all.sql} == {
        ("SELECT", ("tbl_acct_v1",), ()),  # file-level constant
        ("INSERT", (), ("tbl_ledger_audit",)),  # std::string built with += and append
        ("DELETE", (), ("tbl_tmp",)),  # ostringstream
        ("UPDATE", (), ("tbl_host",)),  # embedded EXEC SQL
    }
    main = fn(module, "main")
    assert main.entry_point is not None and main.entry_point.kind == "main"
    assert ("Processor", "create", "call") in calls(main)


# ------------------------------------------------------------------------------ SQL

POSTGRES = """
    -- Core billing schema.

    CREATE TABLE tbl_acct_v1 (
        id BIGINT PRIMARY KEY,
        owner_name VARCHAR(64) NOT NULL,
        balance NUMERIC(12, 2) DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS billing.tbl_ledger_audit (
        id BIGSERIAL PRIMARY KEY,
        acct_id BIGINT REFERENCES tbl_acct_v1(id),
        "Odd Name" text,
        CONSTRAINT fk_a FOREIGN KEY (acct_id) REFERENCES tbl_acct_v1 (id)
    );
    ALTER TABLE tbl_acct_v1 ADD COLUMN region VARCHAR(8) DEFAULT 'EU';

    CREATE OR REPLACE VIEW v_open AS
    SELECT a.id FROM tbl_acct_v1 a JOIN tbl_ledger_audit l ON l.acct_id = a.id;

    -- Writes an audit row whenever a balance changes.
    CREATE OR REPLACE FUNCTION fn_audit_balance() RETURNS trigger AS $$
    DECLARE
        v_delta NUMERIC;
    BEGIN
        v_delta := NEW.balance - OLD.balance;
        IF v_delta <> 0 THEN
            INSERT INTO tbl_ledger_audit (acct_id, delta) VALUES (NEW.id, v_delta);
            PERFORM fn_notify(NEW.id);
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER trg_acct_balance
        AFTER UPDATE OF balance OR INSERT ON tbl_acct_v1
        FOR EACH ROW EXECUTE FUNCTION fn_audit_balance();

    INSERT INTO tbl_acct_v1 (id, owner_name) VALUES (1, 'seed; with semicolon');
    INSERT INTO tbl_acct_v1 (id, owner_name) VALUES (2, 'x');
    COPY tbl_acct_v1 (id, owner_name) FROM stdin;
    3\tfoo; bar
    4\tCREATE TABLE fake (x int);
    \\.
"""


def test_sql_postgres_script() -> None:
    module = parse("db/schema.sql", POSTGRES)
    assert module.docstring == "Core billing schema."
    tables = {t.name: t for t in module.tables}
    assert set(tables) == {"tbl_acct_v1", "tbl_ledger_audit", "v_open"}, (
        "COPY data is not parsed as statements"
    )
    assert [(c.name, c.type) for c in tables["tbl_acct_v1"].columns] == [
        ("id", "BIGINT"),
        ("owner_name", "VARCHAR(64)"),
        ("balance", "NUMERIC(12, 2)"),
        ("region", "VARCHAR(8)"),  # added by ALTER TABLE
    ]
    # the grammar rejects this table's constraint syntax; the lexical fallback still finds the columns
    assert [c.name for c in tables["tbl_ledger_audit"].columns] == ["id", "acct_id", "Odd Name"]
    assert tables["tbl_ledger_audit"].schema == "billing"
    assert tables["v_open"].kind == "view" and tables["v_open"].reads == ("tbl_acct_v1", "tbl_ledger_audit")

    audit = fn(module, "fn_audit_balance")
    assert audit.kind is FunctionKind.PROCEDURE
    assert audit.docstring == "Writes an audit row whenever a balance changes."
    assert audit.return_type == "trigger"
    assert [(s.operation, s.writes) for s in audit.sql] == [("INSERT", ("tbl_ledger_audit",))]
    insert = audit.sql[0]
    assert insert.text.startswith("INSERT INTO tbl_ledger_audit") and insert.line == insert.end_line
    assert module.path and insert.line == audit.start_line + 6  # the INSERT's own line, not the IF above it
    assert [c.name for c in audit.calls] == ["fn_notify"]

    trigger = module.triggers[0]
    assert (trigger.name, trigger.table, trigger.timing, trigger.events) == (
        "trg_acct_balance",
        "tbl_acct_v1",
        "AFTER",
        ("UPDATE", "INSERT"),
    )
    assert [c.name for c in fn(module, "trg_acct_balance").calls] == ["fn_audit_balance"]
    script = fn(module, "<module>")
    assert [(s.operation, s.writes) for s in script.sql] == [("INSERT", ("tbl_acct_v1",))], (
        "seed rows are deduplicated"
    )


def test_sql_mysql_delimiters() -> None:
    module = parse(
        "db/routines.sql",
        """
        DELIMITER //
        CREATE DEFINER=`root`@`localhost` PROCEDURE `sp_mysql`(IN p INT)
        BEGIN
          DECLARE v INT DEFAULT 0;
          SELECT COUNT(*) INTO v FROM orders WHERE id = p;
          IF v > 0 THEN
            UPDATE orders SET seen = 1 WHERE id = p;
          END IF;
        END//
        CREATE TRIGGER trg_orders BEFORE INSERT ON orders FOR EACH ROW
        BEGIN
          INSERT INTO orders_log (order_id) VALUES (NEW.id);
        END//
        DELIMITER ;
        CREATE TABLE `orders` (`id` int(11) NOT NULL AUTO_INCREMENT, `seen` tinyint(1) DEFAULT '0', PRIMARY KEY (`id`));
        """,
    )
    procedure = fn(module, "sp_mysql")
    assert [(p.name, p.type) for p in procedure.params] == [("p", "INT")]
    assert [(s.operation, s.line) for s in procedure.sql] == [("SELECT", 5), ("UPDATE", 7)]
    assert [s.writes for s in fn(module, "trg_orders").sql] == [("orders_log",)]
    assert module.triggers[0].table == "orders" and module.triggers[0].timing == "BEFORE"
    assert [(c.name, c.type) for c in module.tables[0].columns] == [("id", "int(11)"), ("seen", "tinyint(1)")]


def test_sql_tsql_batches() -> None:
    module = parse(
        "db/procs.sql",
        """
        CREATE TABLE [dbo].[Orders] ([Id] INT IDENTITY(1,1) NOT NULL, [Total] DECIMAL(10,2) NULL)
        GO
        CREATE PROCEDURE [dbo].[usp_NightlyRollup] @date DATETIME, @dry BIT = 0
        AS
        SET NOCOUNT ON;
        BEGIN TRANSACTION;
        INSERT INTO dbo.Rollup (Day, Total) SELECT @date, SUM(Total) FROM dbo.Orders WITH (NOLOCK);
        EXEC dbo.usp_Audit @date;
        COMMIT;
        GO
        CREATE TRIGGER trg_Orders_Audit ON dbo.Orders AFTER INSERT, UPDATE AS
        BEGIN
          INSERT INTO dbo.OrdersAudit SELECT * FROM inserted;
        END
        GO
        """,
    )
    assert [(c.name, c.type) for c in module.tables[0].columns] == [("Id", "INT"), ("Total", "DECIMAL(10,2)")]
    rollup = fn(module, "usp_NightlyRollup")
    assert [p.name for p in rollup.params] == ["@date", "@dry"]
    assert [(s.reads, s.writes) for s in rollup.sql] == [(("orders",), ("rollup",))]
    assert "usp_audit" in [c.name for c in rollup.calls]
    trigger = module.triggers[0]
    assert (trigger.table, trigger.timing, trigger.events) == ("Orders", "AFTER", ("INSERT", "UPDATE"))
    assert [(s.reads, s.writes) for s in fn(module, "trg_Orders_Audit").sql] == [((), ("ordersaudit",))]


def test_sql_oracle_package() -> None:
    module = parse(
        "db/pkg_recon.pkb",
        """
        CREATE OR REPLACE PACKAGE BODY pkg_recon AS
          FUNCTION fee(p_amt NUMBER) RETURN NUMBER IS
          BEGIN
            RETURN p_amt * 0.01;
          END fee;

          /* Runs the nightly reconciliation. */
          PROCEDURE run_nightly(p_date IN DATE) IS
            v_cnt NUMBER;
          BEGIN
            SELECT COUNT(*) INTO v_cnt FROM tbl_txn WHERE txn_date = p_date;
            FOR r IN (SELECT id FROM tbl_acct_v1) LOOP
              UPDATE tbl_acct_v1 SET balance = balance - fee(r.id) WHERE id = r.id;
            END LOOP;
          END run_nightly;
        END pkg_recon;
        /
        CREATE OR REPLACE TRIGGER trg_txn BEFORE INSERT ON tbl_txn FOR EACH ROW
        BEGIN
          pkg_recon.run_nightly(SYSDATE);
        END;
        /
        """,
    )
    run = fn(module, "pkg_recon.run_nightly")
    assert run.name == "run_nightly"
    assert run.docstring == "Runs the nightly reconciliation."
    assert {(s.operation, s.reads, s.writes) for s in run.sql} == {
        ("SELECT", ("tbl_txn",), ()),
        ("UPDATE", ("tbl_acct_v1",), ("tbl_acct_v1",)),
    }
    assert "fee" in [c.name for c in run.calls]
    assert fn(module, "pkg_recon.fee").return_type == "NUMBER"
    assert [c.name for c in fn(module, "trg_txn").calls] == ["run_nightly"]


def test_statement_splitter_keeps_bodies_intact() -> None:
    script = "SELECT 1; CREATE FUNCTION f() RETURNS int AS $x$ BEGIN RETURN 1; END; $x$ LANGUAGE plpgsql; SELECT ';' ;"
    assert [s.text[:15] for s in split_statements(script)] == ["SELECT 1", "CREATE FUNCTION", "SELECT ';'"]
    plsql = "CREATE PROCEDURE p IS v NUMBER; BEGIN IF 1=1 THEN NULL; END IF; v := CASE WHEN 1=1 THEN 1 END; END; SELECT 2;"
    assert [s.text.split()[0] for s in split_statements(plsql)] == ["CREATE", "SELECT"]
    assert [s.text for s in split_statements("BEGIN; UPDATE t SET a = 1; COMMIT;")] == [
        "BEGIN",
        "UPDATE t SET a = 1",
        "COMMIT",
    ]
