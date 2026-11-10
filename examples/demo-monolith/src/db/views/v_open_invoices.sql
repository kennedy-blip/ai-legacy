-- Unpaid invoices with the owning account, used by the dunning job.
CREATE OR REPLACE VIEW v_open_invoices AS
SELECT i.id, i.account_id, a.name AS account_name, i.amount, i.due_on, i.dunning_level
FROM tbl_invoice i
JOIN tbl_account a ON a.id = i.account_id
WHERE i.status = 'OPEN';
