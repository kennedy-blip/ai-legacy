"""Cross-file call resolution: what is linked, how confidently, and what is deliberately left alone."""

from __future__ import annotations

import pytest

from ingestion.resolver import DEFAULT_MIN_CONFIDENCE, Resolution, SymbolTable
from parsers.registry import parse_file


class Resolved:
    def __init__(self, files: dict[str, str]) -> None:
        modules = [parse_file(path, text.encode()) for path, text in files.items()]
        self.table = SymbolTable("repo", [module for module in modules if module is not None])
        self.result: Resolution = self.table.resolve()
        self.names = {f.id: f"{f.module.path}:{f.definition.qualified_name}" for f in self.table.functions}
        self.class_names = {c.id: c.definition.qualified_name for c in self.table.classes}
        self.edges = {
            (self.names[call.source], self.names[call.target]): call for call in self.result.calls.values()
        }

    def edge(self, source: str, target: str):
        assert (source, target) in self.edges, f"missing edge {source} -> {target}; have {sorted(self.edges)}"
        return self.edges[source, target]

    def targets(self, source: str) -> set[str]:
        return {target for (origin, target) in self.edges if origin == source}

    def unresolved(self, function: str) -> list[str]:
        ids = {name: node_id for node_id, name in self.names.items()}
        return self.result.unresolved.get(ids[function], [])


# --------------------------------------------------------------------------- Python

PYTHON = {
    "app/__init__.py": "",
    "app/main.py": """
from app.services.billing import BillingService, compute as calc
from app.services import audit
from . import util
from .util import fmt
import app.services.audit
import logging
import requests

def main():
    svc = BillingService(Repo())
    svc.charge(1)
    calc(2)
    audit.record('x')
    app.services.audit.record('y')
    util.fmt(1)
    fmt(2)
    requests.get('http://x')
    logging.getLogger('x')
    unknown_thing(3)

if __name__ == '__main__':
    main()
""",
    "app/util.py": "def fmt(x):\n    return str(x)\n",
    "app/logging.py": "def getLogger(name):\n    return None\n",
    "app/services/__init__.py": "from .billing import BillingService\n",
    "app/services/audit.py": "def record(msg):\n    print(msg)\n",
    "app/services/base.py": """
class BaseService:
    def __init__(self, repo):
        self.repo = repo
    def charge(self, amount):
        self.validate(amount)
        return self._do_charge(amount)
    def validate(self, amount):
        pass
""",
    "app/services/billing.py": """
from .base import BaseService
from .audit import record
from ..repo import Repo
from .queries import FIND_OPEN

def compute(x):
    return x * 2

class BillingService(BaseService):
    def __init__(self, repo: Repo):
        super().__init__(repo)
        self.repo = repo
    def _do_charge(self, amount):
        record('charge')
        self.repo.save(amount)
        cursor.execute(FIND_OPEN)
        return compute(amount)
    def validate(self, amount):
        super().validate(amount)
""",
    "app/services/queries.py": "FIND_OPEN = 'SELECT id FROM tbl_invoice WHERE status = 1'\n",
    "app/repo.py": """
class Repo:
    def save(self, amount):
        return db.execute('INSERT INTO tbl_ledger (amount) VALUES (%s)', (amount,))
""",
}


@pytest.fixture(scope="module")
def python() -> Resolved:
    return Resolved(PYTHON)


def test_python_imports_bind_functions_classes_and_modules(python: Resolved) -> None:
    main = "app/main.py:main"
    assert python.edge(main, "app/services/billing.py:compute").resolution == "import"
    assert python.edge(main, "app/services/billing.py:BillingService.__init__").confidence >= 0.9
    # ``from pkg import module``, ``import a.b.c`` and ``from . import module`` all bind modules.
    assert python.edge(main, "app/services/audit.py:record").lines == [14, 15]
    assert python.edge(main, "app/util.py:fmt").lines == [16, 17]
    assert python.edge("app/main.py:<module>", main).resolution == "same-file"


def test_python_methods_resolve_through_inferred_and_declared_types(python: Resolved) -> None:
    # ``svc = BillingService(...)`` types the local; ``charge`` is inherited from the base class.
    assert (
        python.edge("app/main.py:main", "app/services/base.py:BaseService.charge").resolution
        == "typed-receiver"
    )
    # ``self.repo`` is typed by the constructor parameter annotation.
    edge = python.edge("app/services/billing.py:BillingService._do_charge", "app/repo.py:Repo.save")
    assert (edge.resolution, edge.confidence) == ("field", 0.9)
    assert (
        python.edge(
            "app/services/billing.py:BillingService.validate", "app/services/base.py:BaseService.validate"
        ).resolution
        == "super"
    )
    assert (
        python.edge(
            "app/services/billing.py:BillingService.__init__", "app/services/base.py:BaseService.__init__"
        ).resolution
        == "super"
    )


def test_python_template_method_reaches_the_subclass_hook(python: Resolved) -> None:
    edge = python.edge(
        "app/services/base.py:BaseService.charge", "app/services/billing.py:BillingService._do_charge"
    )
    assert (edge.resolution, edge.confidence) == ("subclass", 0.7)


def test_python_stdlib_and_packages_are_never_bound_to_repository_files(python: Resolved) -> None:
    targets = python.targets("app/main.py:main")
    # ``import logging`` inside package ``app`` is the standard library, not ``app/logging.py``.
    assert "app/logging.py:getLogger" not in targets
    unresolved = python.unresolved("app/main.py:main")
    assert "requests.get" in unresolved
    assert "logging.getLogger" in unresolved
    assert "unknown_thing" in unresolved


def test_python_sql_constant_imported_from_another_module(python: Resolved) -> None:
    (query,) = python.result.constant_queries
    assert python.names[query.function_id] == "app/services/billing.py:BillingService._do_charge"
    assert (query.path, query.name) == ("app/services/queries.py", "FIND_OPEN")
    assert query.statement.reads == ("tbl_invoice",)
    assert (query.statement.line, query.statement.used_at) == (1, 17)


def test_python_overrides_are_flagged(python: Resolved) -> None:
    overrides = {python.names[node_id] for node_id in python.result.overrides}
    assert overrides == {
        "app/services/billing.py:BillingService.__init__",
        "app/services/billing.py:BillingService.validate",
    }
    assert [
        (python.class_names[a], python.class_names[b], kind) for a, b, kind in python.result.inherits
    ] == [("BillingService", "BaseService", "extends")]


# ----------------------------------------------------------------------------- Java

JAVA = {
    "src/com/acme/billing/BillingController.java": """
package com.acme.billing;
import com.acme.billing.service.LedgerService;
import com.acme.billing.dao.*;
import static com.acme.billing.util.Money.round;
import java.util.List;
import org.apache.commons.lang.StringUtils;

public class BillingController extends BaseController {
    private LedgerService ledgerService;
    private AccountDao accountDao;
    private Notifier notifier;

    public void post(Request req) {
        if (StringUtils.isBlank(req.id)) return;
        Account account = accountDao.findById(req.id);
        ledgerService.postCharge(account, round(req.amount));
        this.audit("post");
        notifier.send(account);
        List<Account> all = accountDao.findAll();
        all.forEach(this::touch);
        helperThing.reconcileLedger(account);
        unknownVar.findById(1);
        validate(req);
        String s = req.id;
        s.reconcileLedger();
    }
    private void touch(Account a) {}
}
""",
    "src/com/acme/billing/BaseController.java": """
package com.acme.billing;
public abstract class BaseController {
    protected void audit(String what) { log(what); }
    protected abstract void log(String what);
    protected void validate(Request r) {}
}
""",
    "src/com/acme/billing/service/LedgerService.java": """
package com.acme.billing.service;
import com.acme.billing.dao.AccountDao;
public class LedgerService {
    private AccountDao dao;
    public void postCharge(Account account, long amount) { dao.save(account); reconcileLedger(account); }
    public void postCharge(Account account) { postCharge(account, 0); }
    public void reconcileLedger(Account account) { }
}
""",
    "src/com/acme/billing/dao/AccountDao.java": """
package com.acme.billing.dao;
public interface AccountDao {
    Account findById(String id);
    java.util.List<Account> findAll();
    void save(Account a);
}
""",
    "src/com/acme/billing/dao/JdbcAccountDao.java": """
package com.acme.billing.dao;
public class JdbcAccountDao implements AccountDao {
    public Account findById(String id) { return jdbc.queryForObject("SELECT * FROM tbl_account WHERE id = ?", id); }
    public java.util.List<Account> findAll() { return jdbc.query(Queries.ALL_ACCOUNTS); }
    public void save(Account a) { jdbc.update("UPDATE tbl_account SET balance = ? WHERE id = ?", a.balance, a.id); }
}
""",
    "src/com/acme/billing/dao/Queries.java": """
package com.acme.billing.dao;
final class Queries {
    static final String ALL_ACCOUNTS = "SELECT id, balance FROM tbl_account";
}
""",
    "src/com/acme/billing/util/Money.java": """
package com.acme.billing.util;
public final class Money {
    public static long round(double v) { return Math.round(v); }
}
""",
    "src/com/acme/billing/EmailNotifier.java": """
package com.acme.billing;
public class EmailNotifier implements Notifier {
    public void send(Account a) { }
}
""",
    "src/com/acme/billing/Notifier.java": """
package com.acme.billing;
public interface Notifier { void send(Account a); }
""",
}

_CONTROLLER = "src/com/acme/billing/BillingController.java:BillingController"
_DAO = "src/com/acme/billing/dao"
_SERVICE = "src/com/acme/billing/service/LedgerService.java:LedgerService"


@pytest.fixture(scope="module")
def java() -> Resolved:
    return Resolved(JAVA)


def test_java_field_typed_call_reaches_interface_and_its_implementation(java: Resolved) -> None:
    post = f"{_CONTROLLER}.post"
    declared = java.edge(post, f"{_DAO}/AccountDao.java:AccountDao.findById")
    assert declared.resolution == "field"
    assert declared.confidence >= 0.85
    implementation = java.edge(post, f"{_DAO}/JdbcAccountDao.java:JdbcAccountDao.findById")
    assert (implementation.resolution, implementation.confidence) == ("implementation", 0.85)
    assert (
        java.edge(post, "src/com/acme/billing/EmailNotifier.java:EmailNotifier.send").resolution
        == "implementation"
    )


def test_java_weak_call_site_does_not_borrow_a_strong_edges_lines(java: Resolved) -> None:
    # Line 16 calls through the typed field; line 23 (``unknownVar.findById``) is only a name match.
    edge = java.edge(f"{_CONTROLLER}.post", f"{_DAO}/JdbcAccountDao.java:JdbcAccountDao.findById")
    assert edge.lines == [16]
    assert edge.weak_lines == [23]


def test_java_implicit_this_inherited_static_import_and_method_reference(java: Resolved) -> None:
    post = f"{_CONTROLLER}.post"
    base = "src/com/acme/billing/BaseController.java:BaseController"
    assert java.edge(post, f"{base}.audit").resolution == "inherited"
    assert java.edge(post, f"{base}.validate").resolution == "inherited"
    assert java.edge(post, "src/com/acme/billing/util/Money.java:Money.round").resolution == "static-import"
    touch = java.edge(post, f"{_CONTROLLER}.touch")
    assert touch.confidence == 0.85  # ``this::touch`` is a reference, scored below a direct call
    assert {java.names[node_id] for node_id in java.result.referenced} == {f"{_CONTROLLER}.touch"}
    assert java.edge(f"{base}.audit", f"{base}.log").resolution == "self"


def test_java_overloads_are_distinct_nodes_selected_by_arity(java: Resolved) -> None:
    overloads = [f for f in java.table.functions if f.definition.qualified_name == "LedgerService.postCharge"]
    assert len({f.id for f in overloads}) == 2
    two_args = next(f for f in overloads if len(f.definition.params) == 2)
    one_arg = next(f for f in overloads if len(f.definition.params) == 1)
    calls = java.result.calls
    controller_post = next(
        f for f in java.table.functions if f.definition.qualified_name == "BillingController.post"
    )
    assert (controller_post.id, two_args.id) in calls
    assert (controller_post.id, one_arg.id) not in calls
    delegating = calls[one_arg.id, two_args.id]
    assert delegating.resolution == "self"  # a different overload, not recursion


def test_java_unknown_receiver_falls_back_to_scored_name_matching(java: Resolved) -> None:
    edge = java.edge(f"{_CONTROLLER}.post", f"{_SERVICE}.reconcileLedger")
    assert edge.resolution == "unique-method"
    assert edge.confidence == DEFAULT_MIN_CONFIDENCE
    # ``s`` is a String: a known library type is never name-matched, so line 26 adds nothing.
    assert edge.lines == [22]
    assert edge.weak_lines == []


def test_java_external_calls_are_listed_not_linked(java: Resolved) -> None:
    assert java.unresolved(f"{_CONTROLLER}.post") == ["StringUtils.isBlank", "s.reconcileLedger"]
    (query,) = java.result.constant_queries
    assert (query.path, query.name) == (f"{_DAO}/Queries.java", "ALL_ACCOUNTS")
    assert java.names[query.function_id] == f"{_DAO}/JdbcAccountDao.java:JdbcAccountDao.findAll"


# ----------------------------------------------------------------- JavaScript / TypeScript

JAVASCRIPT = {
    "server/app.js": """
const express = require('express');
const billing = require('./routes/billing');
const { requireAuth, audit: auditLog } = require('./middleware');
const svc = require('./services/ledger');
const Mailer = require('./lib/mailer');
const app = express();
app.use(requireAuth);
app.get('/invoices/:id', billing.show);
app.post('/invoices', requireAuth, async (req, res) => {
  const result = await svc.postCharge(req.body);
  auditLog('post');
  new Mailer().send(result);
  res.json(result);
});
""",
    "server/middleware.js": """
function requireAuth(req, res, next) { next(); }
function audit(msg) { console.log(msg); }
module.exports = { requireAuth, audit };
""",
    "server/routes/billing.js": """
const ledger = require('../services/ledger');
exports.show = function show(req, res) { res.json(ledger.find(req.params.id)); };
exports.list = (req, res) => { res.json(ledger.findAll()); helper(); };
function helper() {}
""",
    "server/services/ledger.js": """
const db = require('../db');
class LedgerService {
  constructor(pool) { this.pool = pool; }
  find(id) { return this.pool.query('SELECT * FROM tbl_ledger WHERE id = $1', [id]); }
  findAll() { return this.find(null); }
  postCharge(body) { this.validate(body); return db.insert('tbl_ledger', body); }
  validate(body) {}
}
module.exports = new LedgerService(db.pool);
""",
    "server/db/index.js": """
const { Pool } = require('pg');
const pool = new Pool();
function insert(table, row) { return pool.query(`INSERT INTO ${table} (amount) VALUES ($1)`, [row.amount]); }
module.exports = { pool, insert };
""",
    "server/lib/mailer.js": """
function Mailer() { this.queue = []; }
Mailer.prototype.send = function (m) { this.flush(); };
Mailer.prototype.flush = function () {};
module.exports = Mailer;
""",
    "web/src/index.ts": """
import { api, fetchJson } from './api';
import Store, { createStore } from './store';
import * as fmt from './lib/format';
import { Button } from '@/components';
import React from 'react';

export async function boot() {
  const s2 = new Store();
  s2.dispatch('x');
  const rows = await api.list();
  await fetchJson('/x');
  fmt.money(1);
  React.createElement(Button);
  createStore();
}
""",
    "web/src/api.ts": """
export async function fetchJson(url: string) { return fetch(url).then(r => r.json()); }
export const api = {
  list: () => fetchJson('/api/list'),
  save(x: number) { return this.list(); },
};
""",
    "web/src/store.ts": """
export default class Store {
  dispatch(action: string) { this.reduce(action); }
  private reduce(action: string) {}
}
export function createStore(): Store { return new Store(); }
""",
    "web/src/lib/format.ts": "export const money = (v: number) => v.toFixed(2);\n",
    "web/src/components/index.ts": "export * from './Button';\n",
    "web/src/components/Button.tsx": "export function Button() { return <button/>; }\n",
}


@pytest.fixture(scope="module")
def javascript() -> Resolved:
    return Resolved(JAVASCRIPT)


def test_js_commonjs_requires_resolve_exports_instances_and_prototypes(javascript: Resolved) -> None:
    route = "server/app.js:POST /invoices"
    # ``module.exports = new LedgerService()``: members of the required object are its methods.
    assert javascript.edge(route, "server/services/ledger.js:LedgerService.postCharge").resolution == "import"
    assert javascript.edge(route, "server/middleware.js:audit").confidence == 0.9
    # Prototype-style "class" required as a constructor.
    assert javascript.edge(route, "server/lib/mailer.js:Mailer.constructor").resolution == "constructor"
    assert javascript.edge(route, "server/lib/mailer.js:Mailer.send").resolution == "typed-receiver"
    assert (
        javascript.edge("server/lib/mailer.js:Mailer.send", "server/lib/mailer.js:Mailer.flush").resolution
        == "self"
    )
    # Directory import resolves to index.js.
    assert (
        javascript.edge(
            "server/services/ledger.js:LedgerService.postCharge", "server/db/index.js:insert"
        ).resolution
        == "import"
    )


def test_js_route_handlers_and_middleware_are_reference_edges(javascript: Resolved) -> None:
    handler = javascript.edge("server/app.js:GET /invoices/:id", "server/routes/billing.js:show")
    assert handler.confidence == 0.81
    assert javascript.edge("server/app.js:POST /invoices", "server/middleware.js:requireAuth").lines == [10]
    # A destructured ``require`` binding passed at module level is still a function reference.
    assert javascript.edge("server/app.js:<module>", "server/middleware.js:requireAuth").lines == [8]
    referenced = {javascript.names[node_id] for node_id in javascript.result.referenced}
    assert {"server/middleware.js:requireAuth", "server/routes/billing.js:show"} <= referenced


def test_ts_es_modules_namespaces_default_exports_and_barrels(javascript: Resolved) -> None:
    boot = "web/src/index.ts:boot"
    assert javascript.edge(boot, "web/src/api.ts:api.list").resolution == "import"
    assert javascript.edge(boot, "web/src/api.ts:fetchJson").resolution == "import"
    assert javascript.edge(boot, "web/src/lib/format.ts:money").resolution == "import"
    assert javascript.edge(boot, "web/src/store.ts:Store.dispatch").resolution == "typed-receiver"
    assert javascript.edge(boot, "web/src/store.ts:createStore").resolution == "import"
    # ``@/components`` -> components/index.ts -> ``export * from './Button'``.
    barrel = javascript.edge(boot, "web/src/components/Button.tsx:Button")
    assert 0.7 < barrel.confidence < 0.9
    # ``this`` inside an object literal refers to its sibling members.
    assert javascript.edge("web/src/api.ts:api.save", "web/src/api.ts:api.list").resolution == "self"
    assert "React.createElement" in javascript.unresolved(boot)


# ------------------------------------------------------------------------------ C++

CPP = {
    "src/ledger.h": """
#pragma once
#include "audit.h"
namespace billing {
class Ledger : public Base {
 public:
  explicit Ledger(Audit* audit);
  int post(int amount);
  void flush();
  virtual int fee(int amount) = 0;
 private:
  Audit* audit_;
};
int clamp(int v);
}
""",
    "src/ledger.cpp": """
#include "ledger.h"
#include "util/math.h"
namespace billing {
static int helper(int x) { return x; }
Ledger::Ledger(Audit* audit) : audit_(audit) {}
int Ledger::post(int amount) {
  helper(amount);
  audit_->record(amount);
  this->flush();
  fee(amount);
  return clamp(round_cents(amount));
}
void Ledger::flush() { sqlite3_exec(db, "DELETE FROM tbl_pending WHERE posted = 1", 0, 0, 0); }
int clamp(int v) { return v; }
}
""",
    "src/audit.h": """
class Audit { public: virtual void record(int amount) = 0; };
class FileAudit : public Audit { public: void record(int amount) override { write_line(amount); } };
""",
    "src/util/math.cpp": '#include "math.h"\nint round_cents(int v) { return v; }\n',
    "src/util/math.h": "int round_cents(int v);\n",
    "src/retail.cpp": """
#include "ledger.h"
class RetailLedger : public billing::Ledger {
 public:
  int fee(int amount) override { return amount / 100; }
};
int main() {
  FileAudit audit;
  RetailLedger ledger(&audit);
  ledger.post(10);
  billing::Ledger* p = &ledger;
  p->flush();
  return 0;
}
""",
}


@pytest.fixture(scope="module")
def cpp() -> Resolved:
    return Resolved(CPP)


def test_cpp_out_of_line_methods_attach_to_the_class_declared_in_the_header(cpp: Resolved) -> None:
    ledger = next(c for c in cpp.table.classes if c.name == "Ledger")
    assert ledger.module.path == "src/ledger.h"
    assert {"post", "flush", "fee", "Ledger"} <= set(ledger.methods)
    assert {m.module.path for m in ledger.methods["post"]} == {"src/ledger.cpp"}
    post = "src/ledger.cpp:Ledger::post"
    assert cpp.edge(post, "src/ledger.cpp:Ledger::flush").resolution == "self"
    assert cpp.edge(post, "src/ledger.cpp:helper").resolution == "same-file"


def test_cpp_virtual_dispatch_and_member_pointer_types(cpp: Resolved) -> None:
    post = "src/ledger.cpp:Ledger::post"
    assert cpp.edge(post, "src/audit.h:Audit::record").resolution == "field"
    assert cpp.edge(post, "src/audit.h:FileAudit::record").resolution == "implementation"
    assert cpp.edge(post, "src/ledger.h:Ledger::fee").resolution == "self"
    assert cpp.edge(post, "src/retail.cpp:RetailLedger::fee").resolution == "implementation"
    main = "src/retail.cpp:main"
    assert cpp.edge(main, "src/ledger.cpp:Ledger::post").resolution == "typed-receiver"
    assert cpp.edge(main, "src/ledger.cpp:Ledger::flush").resolution == "typed-receiver"


def test_cpp_free_function_is_linked_through_the_included_header(cpp: Resolved) -> None:
    edge = cpp.edge("src/ledger.cpp:Ledger::post", "src/util/math.cpp:round_cents")
    assert (edge.resolution, edge.confidence) == ("include", 0.9)
    assert cpp.unresolved("src/ledger.cpp:Ledger::flush") == ["sqlite3_exec"]


# ------------------------------------------------------------------------------ SQL

SQL = {
    "db/procs.sql": """
CREATE OR REPLACE FUNCTION fn_fee(p_amount NUMERIC) RETURNS NUMERIC AS $$
BEGIN
  RETURN p_amount * 0.02;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE PROCEDURE sp_post_charge(p_account INT, p_amount NUMERIC)
LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO tbl_ledger (account_id, amount) VALUES (p_account, p_amount + fn_fee(p_amount));
  CALL sp_notify(p_account);
END;
$$;

CREATE OR REPLACE PROCEDURE sp_notify(p_account INT) LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('billing', p_account::text);
END;
$$;

CREATE FUNCTION trg_ledger_audit_fn() RETURNS trigger AS $$
BEGIN
  INSERT INTO tbl_ledger_audit (ledger_id, changed_at) VALUES (NEW.id, now());
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_ledger_audit AFTER INSERT OR UPDATE ON tbl_ledger
FOR EACH ROW EXECUTE FUNCTION trg_ledger_audit_fn();
""",
    "app/billing.py": """
def post(cur, account, amount):
    cur.execute("CALL dbo.sp_post_charge(%s, %s)", (account, amount))
    cur.execute("SELECT fn_fee(amount), upper(name) FROM tbl_ledger WHERE account_id = %s", (account,))
    cur.callproc("sp_notify", [account])
""",
}


@pytest.fixture(scope="module")
def sql() -> Resolved:
    return Resolved(SQL)


def test_sql_routines_call_each_other_and_builtins_are_ignored(sql: Resolved) -> None:
    assert sql.targets("db/procs.sql:sp_post_charge") == {"db/procs.sql:fn_fee", "db/procs.sql:sp_notify"}
    assert sql.targets("db/procs.sql:sp_notify") == set()
    assert sql.targets("db/procs.sql:trg_ledger_audit") == {"db/procs.sql:trg_ledger_audit_fn"}
    assert sql.result.unresolved.keys() <= {
        node_id for node_id, name in sql.names.items() if name.startswith("app/")
    }


def test_application_code_calls_stored_routines_through_embedded_sql(sql: Resolved) -> None:
    post = "app/billing.py:post"
    explicit = sql.edge(post, "db/procs.sql:sp_post_charge")  # schema-qualified CALL
    assert (explicit.resolution, explicit.confidence, explicit.lines) == ("sql-call", 0.9, [3])
    scalar = sql.edge(post, "db/procs.sql:fn_fee")
    assert (scalar.resolution, scalar.confidence) == ("sql-function", 0.8)
    by_name = sql.edge(post, "db/procs.sql:sp_notify")  # cursor.callproc("sp_notify")
    assert (by_name.resolution, by_name.confidence, by_name.lines) == ("name-reference", 0.7, [5])
    # A trigger cannot be invoked by name.
    assert "db/procs.sql:trg_ledger_audit" not in sql.targets(post)


# ------------------------------------------------------------------------- invariants


def test_callback_references_are_never_matched_by_global_name() -> None:
    resolved = Resolved(
        {
            "a.js": "function run(items) { items.forEach(process); emitter.on('x', obj.handle); }\n",
            "b.js": "function process(x) {}\nclass Obj { handle() {} }\n",
        }
    )
    assert resolved.targets("a.js:run") == set()


def test_ambiguous_names_stay_hidden_or_unlinked() -> None:
    files = {f"m{i}.py": "def helper():\n    pass\n" for i in range(3)}
    files["caller.py"] = "def go():\n    helper()\n"
    resolved = Resolved(files)
    edges = [edge for (source, _), edge in resolved.edges.items() if source == "caller.py:go"]
    assert len(edges) == 3
    assert all(edge.resolution == "ambiguous" and edge.confidence < DEFAULT_MIN_CONFIDENCE for edge in edges)

    files["m3.py"] = "def helper():\n    pass\n"
    assert Resolved(files).targets("caller.py:go") == set()  # four candidates: no evidence at all


def test_recursion_is_labelled() -> None:
    resolved = Resolved({"r.py": "def walk(n):\n    return walk(n - 1) if n else 0\n"})
    assert resolved.edge("r.py:walk", "r.py:walk").resolution == "recursion"


def test_duplicate_definitions_get_distinct_ids() -> None:
    resolved = Resolved({"d.py": "def f():\n    pass\n\ndef f():\n    pass\n"})
    ids = [f.id for f in resolved.table.functions if f.definition.name == "f"]
    assert len(ids) == len(set(ids)) == 2
