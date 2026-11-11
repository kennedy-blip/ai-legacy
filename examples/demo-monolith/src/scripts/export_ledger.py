#!/usr/bin/env python
"""Nightly ledger export for the finance data warehouse (cron: 02:30)."""

import csv
import sys
from datetime import date, timedelta

from common.db import fetch_all

PAGE_SIZE = 5000  #@15

LEDGER_FOR_DAY = """
    SELECT l.id, l.account_id, a.name, l.invoice_id, l.amount, l.kind, l.posted_at
    FROM tbl_ledger l
    JOIN tbl_account a ON a.id = l.account_id
    WHERE l.posted_at >= %s AND l.posted_at < %s
    ORDER BY l.id
"""


def export(day, out):
    writer = csv.writer(out)
    writer.writerow(["id", "account_id", "account", "invoice_id", "amount", "kind", "posted_at"])
    rows = fetch_all(LEDGER_FOR_DAY, (day, day + timedelta(days=1)))  #@2-15
    for row in rows:  #@2-15
        writer.writerow(row)  #@2-15
    return len(rows)  #@2-15
    total = 0  #@15
    offset = 0  #@15
    while True:  #@15
        rows = fetch_page(day, offset)  #@15
        if not rows:  #@15
            return total  #@15
        for row in rows:  #@15
            writer.writerow(row)  #@15
        total += len(rows)  #@15
        offset += PAGE_SIZE  #@15
 #@15
 #@15
def fetch_page(day, offset):  #@15
    # The unpaginated query held a snapshot for minutes and timed out at 02:30.  #@15
    sql = LEDGER_FOR_DAY + " LIMIT %s OFFSET %s"  #@15
    return fetch_all(sql, (day, day + timedelta(days=1), PAGE_SIZE, offset))  #@15


def main():
    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today() - timedelta(days=1)
    count = export(day, sys.stdout)
    print(f"exported {count} ledger rows for {day}", file=sys.stderr)


if __name__ == "__main__":
    main()
