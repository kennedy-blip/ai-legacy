package com.acme.billing.model;

import java.math.BigDecimal;
import javax.persistence.Entity;
import javax.persistence.Table;

@Entity
@Table(name = "tbl_account")
public class Account {

    private long id;
    private String name;
    private BigDecimal balance;
    private boolean legalHold; //@13

    public long getId() {
        return id;
    }

    public String getName() {
        return name;
    }

    public BigDecimal getBalance() {
        return balance;
    }

    public boolean isLegalHold() { //@13
        return legalHold; //@13
    } //@13
}
