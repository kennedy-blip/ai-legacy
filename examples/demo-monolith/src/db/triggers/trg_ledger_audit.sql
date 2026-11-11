-- SOX control C-17: every insert or update of the ledger leaves an audit row.
-- The application does not know about this table; do not rely on it being empty.
CREATE OR REPLACE FUNCTION fn_ledger_audit() RETURNS trigger AS $$
BEGIN
    INSERT INTO tbl_ledger_audit (ledger_id, action, amount, changed_by, changed_at)
    VALUES (NEW.id, TG_OP, NEW.amount, current_user, now());
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_ledger_audit
AFTER INSERT OR UPDATE ON tbl_ledger
FOR EACH ROW EXECUTE FUNCTION fn_ledger_audit();
