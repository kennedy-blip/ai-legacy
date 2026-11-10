"""Wording of payment reminders. Level 3 is reviewed by legal; do not edit freely."""

SUBJECTS = {
    1: "Friendly reminder: invoice {id} is overdue",
    2: "Second reminder: invoice {id}",
    3: "Final notice before collection: invoice {id}",
}


def subject(level, invoice_id):
    return SUBJECTS.get(level, SUBJECTS[3]).format(id=invoice_id)


def render(level, invoice):
    lines = [
        f"Dear {invoice['account_name']},",
        f"our records show that invoice {invoice['id']} over {invoice['amount']} was due on {invoice['due_on']}.",
    ]
    if level >= 3:
        lines.append("If payment is not received within 7 days the account is handed to collections.")
    return "\n".join(lines)
