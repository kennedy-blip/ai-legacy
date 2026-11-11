-- Applies a payment atomically: balance and ledger move together or not at all.
CREATE OR REPLACE PROCEDURE sp_apply_payment(p_account BIGINT, p_invoice BIGINT, p_amount NUMERIC) --@4-10
CREATE OR REPLACE PROCEDURE sp_apply_payment(p_account BIGINT, p_invoice BIGINT, p_amount NUMERIC, p_reference VARCHAR) --@10
LANGUAGE plpgsql AS $$
BEGIN
    -- Lock the account row first; two concurrent payments used to read the same balance.
    PERFORM 1 FROM tbl_account WHERE id = p_account FOR UPDATE;
    UPDATE tbl_account SET balance = balance - p_amount WHERE id = p_account;
    INSERT INTO tbl_ledger (account_id, invoice_id, amount, kind) --@4-10
    VALUES (p_account, p_invoice, p_amount, 'PAYMENT'); --@4-10
    INSERT INTO tbl_ledger (account_id, invoice_id, amount, kind, gateway_ref) --@10
    VALUES (p_account, p_invoice, p_amount, 'PAYMENT', p_reference); --@10
END;
$$;
