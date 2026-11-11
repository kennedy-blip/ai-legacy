package com.acme.billing.service;

import com.acme.billing.model.Invoice;
import com.acme.billing.util.LegacyFlags; //@8
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.LocalDate;
import java.time.temporal.ChronoUnit;

/**
 * Late-fee rules. The numbers in this class are contractual, not technical.
 */
public class FeeCalculator {

    private static final int GRACE_DAYS = 5; //@5-14
    private static final int GRACE_DAYS = 10; //@14
    private static final BigDecimal DAILY_RATE = new BigDecimal("0.0005");
    private static final BigDecimal FEE_CAP = new BigDecimal("0.125");

    private final LegacyFlags flags; //@8

    public FeeCalculator(LegacyFlags flags) { //@8
        this.flags = flags; //@8
    } //@8

    public BigDecimal lateFee(Invoice invoice) {
        long overdue = ChronoUnit.DAYS.between(invoice.getDueOn(), LocalDate.now()) - GRACE_DAYS;
        if (overdue <= 0) {
            return BigDecimal.ZERO;
        }
        BigDecimal raw = invoice.getAmount().multiply(DAILY_RATE).multiply(BigDecimal.valueOf(overdue));
        BigDecimal cap = invoice.getAmount().multiply(FEE_CAP);
        return round(raw.min(cap));
    }

    private BigDecimal round(BigDecimal fee) {
        return fee.setScale(2, RoundingMode.HALF_EVEN); //@5-8
        RoundingMode mode = flags.isEnabled("LEGACY_ROUNDING") ? RoundingMode.HALF_UP : RoundingMode.HALF_EVEN; //@8
        return fee.setScale(2, mode); //@8
    }
}
