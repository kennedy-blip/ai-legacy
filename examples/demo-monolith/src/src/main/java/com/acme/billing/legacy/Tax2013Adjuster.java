package com.acme.billing.legacy;

import java.math.BigDecimal;
import java.time.LocalDate;

/**
 * Invoices issued before the 2013 VAT change carry the old rate; payments
 * against them are scaled so the ledger shows the new-rate equivalent.
 */
public final class Tax2013Adjuster {

    private static final LocalDate CUTOVER = LocalDate.of(2013, 4, 1);
    private static final BigDecimal FACTOR = new BigDecimal("1.0083");

    private Tax2013Adjuster() {
    }

    public static BigDecimal adjust(BigDecimal amount, LocalDate issuedOn) {
        if (issuedOn.isBefore(CUTOVER)) {
            return scale(amount);
        }
        return amount;
    }

    private static BigDecimal scale(BigDecimal amount) {
        return amount.multiply(FACTOR).setScale(2, BigDecimal.ROUND_HALF_UP);
    }
}
