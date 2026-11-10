package com.acme.billing.dao;

import java.math.BigDecimal;
import org.springframework.jdbc.core.JdbcTemplate;

public class LedgerDao {

    private final JdbcTemplate jdbc;

    public LedgerDao(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public void insertEntry(long accountId, long invoiceId, BigDecimal amount, String kind) { //@1-10
        jdbc.update( //@1-10
                "INSERT INTO tbl_ledger (account_id, invoice_id, amount, kind) VALUES (?, ?, ?, ?)", //@1-10
                accountId, invoiceId, amount, kind); //@1-10
    public void insertEntry(long accountId, long invoiceId, BigDecimal amount, String kind, String reference) { //@10
        jdbc.update( //@10
                "INSERT INTO tbl_ledger (account_id, invoice_id, amount, kind, gateway_ref) VALUES (?, ?, ?, ?, ?)", //@10
                accountId, invoiceId, amount, kind, reference); //@10
    }

    public boolean existsByReference(String reference) { //@10
        Integer count = jdbc.queryForObject( //@10
                "SELECT COUNT(*) FROM tbl_ledger WHERE gateway_ref = ?", Integer.class, reference); //@10
        return count != null && count > 0; //@10
    } //@10
}
