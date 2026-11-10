-- Core billing schema. Applied by the DBA team; the application never runs DDL.

CREATE TABLE tbl_account (
    id          BIGINT PRIMARY KEY,
    name        VARCHAR(200) NOT NULL,
    balance     NUMERIC(14, 2) NOT NULL DEFAULT 0,
    legal_hold  SMALLINT NOT NULL DEFAULT 0, --@13
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE tbl_invoice (
    id            BIGINT PRIMARY KEY,
    account_id    BIGINT NOT NULL REFERENCES tbl_account (id),
    amount        NUMERIC(14, 2) NOT NULL,
    status        VARCHAR(16) NOT NULL DEFAULT 'OPEN',
    issued_on     DATE NOT NULL,
    due_on        DATE NOT NULL,
    paid_on       DATE --@1-11
    paid_on       DATE, --@11
    dunning_level SMALLINT NOT NULL DEFAULT 0 --@11
);

CREATE TABLE tbl_ledger (
    id          BIGSERIAL PRIMARY KEY,
    account_id  BIGINT NOT NULL REFERENCES tbl_account (id),
    invoice_id  BIGINT REFERENCES tbl_invoice (id),
    amount      NUMERIC(14, 2) NOT NULL,
    kind        VARCHAR(16) NOT NULL,
    gateway_ref VARCHAR(64), --@10
    period_id   INTEGER, --@7
    posted_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE tbl_ledger_audit ( --@6
    id          BIGSERIAL PRIMARY KEY, --@6
    ledger_id   BIGINT NOT NULL, --@6
    action      VARCHAR(8) NOT NULL, --@6
    amount      NUMERIC(14, 2), --@6
    changed_by  VARCHAR(64) NOT NULL, --@6
    changed_at  TIMESTAMP NOT NULL --@6
); --@6
 --@6
CREATE TABLE tbl_period ( --@7
    id          SERIAL PRIMARY KEY, --@7
    closed_on   DATE NOT NULL, --@7
    total       NUMERIC(16, 2) NOT NULL --@7
); --@7
 --@7
CREATE TABLE tbl_recon_exception ( --@7
    id          BIGSERIAL PRIMARY KEY, --@7
    invoice_id  BIGINT NOT NULL, --@7
    expected    NUMERIC(14, 2) NOT NULL, --@7
    posted      NUMERIC(14, 2) NOT NULL, --@7
    detected_at TIMESTAMP NOT NULL --@7
); --@7
 --@7
CREATE TABLE tbl_feature_flag ( --@8
    name        VARCHAR(64) PRIMARY KEY, --@8
    enabled     SMALLINT NOT NULL DEFAULT 0, --@8
    changed_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP --@8
); --@8
