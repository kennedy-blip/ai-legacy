#!/usr/bin/env python3
"""Materialise the synthetic ``core-billing-monolith`` demo repository with its git history.

The demo is **fabricated** to exercise Archaeo-Code end to end: no real company,
people, tickets or incidents are described. Its purpose is a small code base whose
history contains the things the platform is built to surface - business rules
explained only in commit messages, a hidden database trigger, dead code, several
generations of authors.

How it works
------------
``src/`` holds one annotated copy of every file. A trailing marker records in which
commit a line appeared and, optionally, in which commit it was removed again::

    return fee.setScale(2, RoundingMode.HALF_EVEN); //@5-8     present in commits 5, 6 and 7
    return fee.setScale(2, mode); //@8                         present from commit 8 on

Unmarked lines exist from the commit that adds the file. Replaying the commits in
order therefore yields real, line-accurate ``git blame`` output from a single
source of truth. Markers use the comment syntax of the file (``//@``, ``--@``, ``#@``)
and are stripped from the generated files.

Usage::

    python examples/demo-monolith/build_history.py /tmp/core-billing-monolith
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "src"
MARKER_RE = re.compile(r"[ \t]*(?://|--|#)@(\d+)(?:-(\d+))?[ \t]*$")

MARGARET = ("Margaret Okafor", "margaret.okafor@acme-billing.example")
DIETER = ("Dieter Brandt", "dieter.brandt@acme-billing.example")
PRIYA = ("Priya Raman", "priya.raman@acme-billing.example")
TOMAS = ("Tomás Herrera", "tomas.herrera@acme-billing.example")

JAVA = "src/main/java/com/acme/billing"


@dataclass(frozen=True)
class Commit:
    index: int
    date: str
    author: tuple[str, str]
    subject: str
    body: str = ""
    adds: tuple[str, ...] = field(default_factory=tuple)


COMMITS: tuple[Commit, ...] = (
    Commit(
        1,
        "2012-03-14T09:12:00+00:00",
        MARGARET,
        "Initial import of billing core from SVN r4812",
        "History before this point lives in the old Subversion server (decommissioned).",
        adds=(
            f"{JAVA}/web/InvoiceController.java",
            f"{JAVA}/web/AccountController.java",
            f"{JAVA}/web/PaymentReceipt.java",
            f"{JAVA}/web/PaymentRequest.java",
            f"{JAVA}/service/PaymentService.java",
            f"{JAVA}/dao/AccountDao.java",
            f"{JAVA}/dao/JdbcAccountDao.java",
            f"{JAVA}/dao/InvoiceDao.java",
            f"{JAVA}/dao/LedgerDao.java",
            f"{JAVA}/model/Invoice.java",
            f"{JAVA}/model/Account.java",
            "db/schema/001_tables.sql",
        ),
    ),
    Commit(
        2,
        "2012-09-03T14:40:00+00:00",
        MARGARET,
        "Add nightly ledger export for the finance warehouse (BILL-212)",
        "Finance loads the CSV at 03:00; the cron entry runs this at 02:30.",
        adds=("scripts/export_ledger.py", "scripts/common/__init__.py", "scripts/common/db.py"),
    ),
    Commit(
        3,
        "2013-01-22T10:05:00+00:00",
        DIETER,
        "BILL-688: scale payments on pre-2013 invoices for the VAT transition",
        "Invoices issued before 2013-04-01 carry the old VAT rate. Until they are all\n"
        "settled, payments against them are multiplied by 1.0083 so the ledger shows the\n"
        "new-rate equivalent. Temporary: remove once the last old-rate invoice is paid.",
        adds=(f"{JAVA}/legacy/Tax2013Adjuster.java",),
    ),
    Commit(
        4,
        "2013-11-05T16:31:00+00:00",
        MARGARET,
        "Move balance update into sp_apply_payment to stop lost updates (BILL-901)",
        "Two payments for the same account within the same second both read the old\n"
        "balance in Java and the second write won. The stored procedure locks the account\n"
        "row and updates balance and ledger in a single transaction.",
        adds=("db/procs/sp_apply_payment.sql",),
    ),
    Commit(
        5,
        "2014-06-18T11:47:00+00:00",
        DIETER,
        "BILL-1432: late fees - 0.05%/day after grace period, capped at 12.5%, rounded half-even",
        "Auditor finding FA-2014-07: fees were rounded half-up, which systematically\n"
        "favoured us by a cent on .5 amounts. Contract clause 9.3 fixes the daily rate\n"
        "(0.0005) and the cap (12.5% of the invoice amount).\n\n"
        "Do NOT change these numbers without written sign-off from Finance.",
        adds=(f"{JAVA}/service/FeeCalculator.java",),
    ),
    Commit(
        6,
        "2015-02-09T09:58:00+00:00",
        MARGARET,
        "Audit every ledger write with a database trigger (SOX control C-17) (#41)",
        "The control requires an audit record that application code cannot skip, so it\n"
        "is implemented as a trigger on tbl_ledger rather than in the DAO.",
        adds=("db/triggers/trg_ledger_audit.sql",),
    ),
    Commit(
        7,
        "2016-04-27T15:20:00+00:00",
        MARGARET,
        "Nightly reconciliation job and month-end close (BILL-2101)",
        "Compares paid invoices with posted ledger rows at 02:00 and records mismatches\n"
        "in tbl_recon_exception. On the first of the month sp_close_period stamps the\n"
        "open ledger rows with a new period.",
        adds=(f"{JAVA}/service/NightlyReconciliationJob.java", "db/procs/sp_close_period.sql"),
    ),
    Commit(
        8,
        "2017-08-15T13:02:00+00:00",
        PRIYA,
        "Feature flags in the database; LEGACY_ROUNDING restores half-up fees (BILL-2677) (#88)",
        "Two enterprise customers have contracts that predate BILL-1432 and still\n"
        "specify half-up rounding. Operations can enable LEGACY_ROUNDING for their\n"
        "deployment without a release. Default stays half-even.",
        adds=(f"{JAVA}/util/LegacyFlags.java",),
    ),
    Commit(
        9,
        "2018-03-02T10:15:00+00:00",
        DIETER,
        "Stop applying the 2013 VAT adjustment (BILL-2890)",
        "The last old-rate invoice was settled in 2014. Tax2013Adjuster is no longer\n"
        "called; leaving the class in place until Finance confirms no reprocessing is needed.",
    ),
    Commit(
        10,
        "2019-10-21T08:44:00+00:00",
        PRIYA,
        "BILL-3312: idempotency guard for gateway callbacks (#132)",
        "Incident INC-5521: the payment gateway retried a callback after a timeout and\n"
        "the payment was posted twice. applyPayment now refuses a gateway reference that\n"
        "already exists in tbl_ledger, and the reference is stored with every entry.",
    ),
    Commit(
        11,
        "2020-05-11T12:30:00+00:00",
        TOMAS,
        "Dunning reminders: Python job on top of v_open_invoices (BILL-3540) (#151)",
        "Three escalation levels, tracked in tbl_invoice.dunning_level. Level 3 wording\n"
        "was approved by Legal.",
        adds=(
            "db/views/v_open_invoices.sql",
            "scripts/dunning/__init__.py",
            "scripts/dunning/send_reminders.py",
            "scripts/dunning/templates.py",
        ),
    ),
    Commit(
        12,
        "2021-02-18T17:09:00+00:00",
        PRIYA,
        "Extract lookup SQL into Queries constants; no behaviour change (#167)",
        adds=(f"{JAVA}/dao/Queries.java",),
    ),
    Commit(
        13,
        "2022-09-07T09:26:00+00:00",
        TOMAS,
        "BILL-4102: skip accounts on legal hold in the nightly reconciliation",
        "Compliance request LH-22: accounts under legal hold must not generate\n"
        "reconciliation exceptions, because exceptions trigger automated correction\n"
        "entries. Set -Dbilling.recon.includeLegalHold=true to include them again.",
    ),
    Commit(
        14,
        "2023-06-30T14:55:00+00:00",
        TOMAS,
        "Raise late-fee grace period from 5 to 10 days (BILL-4388) (#203)",
        "Signed off by Finance (ticket FIN-771) after the 2023 terms update.",
    ),
    Commit(
        15,
        "2024-01-16T07:38:00+00:00",
        TOMAS,
        "export_ledger: paginate the export, the 02:30 run was timing out (BILL-4521)",
        "The single query held a snapshot for several minutes once the ledger passed\n"
        "40M rows and was killed by the statement timeout.",
    ),
)


def render(text: str, index: int) -> str:
    """The content of an annotated file as of commit ``index``."""
    kept: list[str] = []
    for line in text.split("\n"):
        marker = MARKER_RE.search(line)
        if marker is None:
            kept.append(line)
            continue
        first = int(marker.group(1))
        last = int(marker.group(2)) if marker.group(2) else None
        if first <= index and (last is None or index < last):
            kept.append(line[: marker.start()])
    return "\n".join(kept)


def export_head(target: Path) -> int:
    """Write every file as it stands after the last commit into ``target``; no git involved.

    The result is byte-identical to the working tree :func:`build` leaves behind, so a
    graph computed from the repository lines up with these files. Returns the file count.
    """
    last = COMMITS[-1].index
    paths = [path for commit in COMMITS for path in commit.adds]
    for path in paths:
        destination = target / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(render((SOURCE / path).read_text(encoding="utf-8"), last), encoding="utf-8")
    return len(paths)


def _git(target: Path, *args: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=target,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def build(target: Path, *, force: bool = False) -> list[str]:
    """Create the repository at ``target`` and return the commit hashes, oldest first."""
    if target.exists():
        if not force:
            raise FileExistsError(f"{target} already exists (use --force to replace it)")
        shutil.rmtree(target)
    target.mkdir(parents=True)
    _git(target, "init", "--quiet", "--initial-branch=main")
    _git(target, "config", "commit.gpgsign", "false")
    _git(target, "config", "core.autocrlf", "false")

    created: dict[str, int] = {}
    hashes: list[str] = []
    for commit in COMMITS:
        for path in commit.adds:
            if not (SOURCE / path).is_file():
                raise FileNotFoundError(f"commit {commit.index} adds {path}, which is missing from src/")
            created[path] = commit.index
        changed = 0
        for path in created:
            content = render((SOURCE / path).read_text(encoding="utf-8"), commit.index)
            destination = target / path
            if destination.exists() and destination.read_text(encoding="utf-8") == content:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
            changed += 1
        if not changed:
            raise RuntimeError(f"commit {commit.index} ({commit.subject!r}) changes nothing")
        name, email = commit.author
        env = {
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_AUTHOR_DATE": commit.date,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
            "GIT_COMMITTER_DATE": commit.date,
        }
        message = commit.subject + (f"\n\n{commit.body}" if commit.body else "")
        _git(target, "add", "--all")
        _git(target, "commit", "--quiet", "--no-verify", "-m", message, env=env)
        hashes.append(_git(target, "rev-parse", "HEAD"))

    unreferenced = sorted(
        str(path.relative_to(SOURCE))
        for path in SOURCE.rglob("*")
        if path.is_file() and str(path.relative_to(SOURCE)) not in created
    )
    if unreferenced:
        raise RuntimeError(f"files in src/ that no commit adds: {unreferenced}")
    return hashes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("target", type=Path, help="directory to create the repository in")
    parser.add_argument("--force", action="store_true", help="replace the target if it exists")
    arguments = parser.parse_args(argv)
    hashes = build(arguments.target, force=arguments.force)
    print(f"created {arguments.target} with {len(hashes)} commits ({hashes[0][:8]}..{hashes[-1][:8]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
