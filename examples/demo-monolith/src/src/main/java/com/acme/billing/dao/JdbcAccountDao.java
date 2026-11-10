package com.acme.billing.dao;

import com.acme.billing.model.Account;
import java.math.BigDecimal;
import org.springframework.jdbc.core.JdbcTemplate;

public class JdbcAccountDao implements AccountDao {

    private final JdbcTemplate jdbc;

    public JdbcAccountDao(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @Override
    public Account findById(long id) {
        return jdbc.queryForObject( //@1-12
                "SELECT id, name, balance FROM tbl_account WHERE id = ?", Account.class, id); //@1-12
        return jdbc.queryForObject(Queries.ACCOUNT_BY_ID, Account.class, id); //@12
    }

    /**
     * Direct balance update. Superseded by sp_apply_payment for payments;
     * still used by manual credit notes.
     */
    @Override
    public void debit(long accountId, BigDecimal amount) {
        jdbc.update("UPDATE tbl_account SET balance = balance - ? WHERE id = ?", amount, accountId);
    }
}
