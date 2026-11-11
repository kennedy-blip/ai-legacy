-- Month-end close: stamps every unassigned ledger row with a new period.
CREATE OR REPLACE PROCEDURE sp_close_period()
LANGUAGE plpgsql AS $$
DECLARE
    v_period INTEGER;
BEGIN
    INSERT INTO tbl_period (closed_on, total)
    SELECT CURRENT_DATE, COALESCE(SUM(amount), 0) FROM tbl_ledger WHERE period_id IS NULL
    RETURNING id INTO v_period;

    UPDATE tbl_ledger SET period_id = v_period WHERE period_id IS NULL;
END;
$$;
