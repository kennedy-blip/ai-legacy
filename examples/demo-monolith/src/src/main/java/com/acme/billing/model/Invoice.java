package com.acme.billing.model;

import java.math.BigDecimal;
import java.time.LocalDate;
import javax.persistence.Entity;
import javax.persistence.Table;

@Entity
@Table(name = "tbl_invoice")
public class Invoice {

    private long id;
    private long accountId;
    private BigDecimal amount;
    private String status;
    private LocalDate issuedOn;
    private LocalDate dueOn;

    public long getId() {
        return id;
    }

    public long getAccountId() {
        return accountId;
    }

    public BigDecimal getAmount() {
        return amount;
    }

    public String getStatus() {
        return status;
    }

    public LocalDate getIssuedOn() {
        return issuedOn;
    }

    public LocalDate getDueOn() {
        return dueOn;
    }
}
