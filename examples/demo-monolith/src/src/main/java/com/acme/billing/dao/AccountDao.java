package com.acme.billing.dao;

import com.acme.billing.model.Account;
import java.math.BigDecimal;

public interface AccountDao {

    Account findById(long id);

    void debit(long accountId, BigDecimal amount);
}
