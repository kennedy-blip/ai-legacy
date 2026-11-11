package com.acme.billing.web;

import java.math.BigDecimal;

public class PaymentReceipt {

    private final long invoiceId;
    private final BigDecimal applied;
    private final BigDecimal fee;
    private final boolean duplicate;

    public PaymentReceipt(long invoiceId, BigDecimal applied, BigDecimal fee) {
        this(invoiceId, applied, fee, false);
    }

    private PaymentReceipt(long invoiceId, BigDecimal applied, BigDecimal fee, boolean duplicate) {
        this.invoiceId = invoiceId;
        this.applied = applied;
        this.fee = fee;
        this.duplicate = duplicate;
    }

    public static PaymentReceipt duplicate(long invoiceId, String reference) {
        return new PaymentReceipt(invoiceId, BigDecimal.ZERO, BigDecimal.ZERO, true);
    }

    public long getInvoiceId() {
        return invoiceId;
    }

    public BigDecimal getApplied() {
        return applied;
    }

    public BigDecimal getFee() {
        return fee;
    }

    public boolean isDuplicate() {
        return duplicate;
    }
}
