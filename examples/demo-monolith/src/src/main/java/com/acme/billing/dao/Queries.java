package com.acme.billing.dao;

/** SQL text shared by the DAOs. */
final class Queries {

    static final String ACCOUNT_BY_ID =
            "SELECT id, name, balance FROM tbl_account WHERE id = ?"; //@12-13
            "SELECT id, name, balance, legal_hold FROM tbl_account WHERE id = ?"; //@13

    static final String INVOICE_BY_ID =
            "SELECT id, account_id, amount, status, issued_on, due_on FROM tbl_invoice WHERE id = ?";

    private Queries() {
    }
}
