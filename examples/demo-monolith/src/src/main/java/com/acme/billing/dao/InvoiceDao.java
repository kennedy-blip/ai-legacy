package com.acme.billing.dao;

import com.acme.billing.model.Invoice;
import org.springframework.jdbc.core.JdbcTemplate;

public class InvoiceDao {

    private final JdbcTemplate jdbc;

    public InvoiceDao(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public Invoice findById(long id) {
        return jdbc.queryForObject( //@1-12
                "SELECT id, account_id, amount, status, issued_on, due_on FROM tbl_invoice WHERE id = ?", //@1-12
                Invoice.class, id); //@1-12
        return jdbc.queryForObject(Queries.INVOICE_BY_ID, Invoice.class, id); //@12
    }

    public void markPaid(long id) {
        jdbc.update("UPDATE tbl_invoice SET status = 'PAID', paid_on = CURRENT_DATE WHERE id = ?", id);
    }
}
