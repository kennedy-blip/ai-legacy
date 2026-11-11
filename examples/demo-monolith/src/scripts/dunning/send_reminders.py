#!/usr/bin/env python
"""Sends escalating payment reminders for overdue invoices (cron: 06:00, weekdays)."""

import smtplib
from datetime import date
from email.message import EmailMessage

from common.db import execute, fetch_all
from dunning import templates

MAX_LEVEL = 3


def overdue_invoices(today):
    rows = fetch_all(
        "SELECT id, account_id, account_name, amount, due_on, dunning_level "
        "FROM v_open_invoices WHERE due_on < %s AND dunning_level < %s",
        (today, MAX_LEVEL),
    )
    keys = ("id", "account_id", "account_name", "amount", "due_on", "dunning_level")
    return [dict(zip(keys, row)) for row in rows]


def escalate(invoice):
    level = invoice["dunning_level"] + 1
    message = EmailMessage()
    message["Subject"] = templates.subject(level, invoice["id"])
    message.set_content(templates.render(level, invoice))
    deliver(message)
    execute("UPDATE tbl_invoice SET dunning_level = %s WHERE id = %s", (level, invoice["id"]))
    return level


def deliver(message):
    with smtplib.SMTP("localhost") as smtp:
        smtp.send_message(message)


def main():
    for invoice in overdue_invoices(date.today()):
        escalate(invoice)


if __name__ == "__main__":
    main()
