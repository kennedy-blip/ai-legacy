package com.acme.billing.service;

import com.acme.billing.dao.AccountDao;
import com.acme.billing.dao.InvoiceDao;
import com.acme.billing.dao.LedgerDao;
import com.acme.billing.legacy.Tax2013Adjuster; //@3-9
import com.acme.billing.model.Invoice;
import com.acme.billing.web.PaymentReceipt;
import java.math.BigDecimal;
import java.sql.CallableStatement;
import java.sql.Connection;
import java.sql.SQLException;
import javax.sql.DataSource;

/**
 * Applies customer payments to invoices and the general ledger.
 */
public class PaymentService {

    private final DataSource dataSource;
    private final InvoiceDao invoiceDao;
    private final LedgerDao ledgerDao;
    private final AccountDao accountDao;
    private final FeeCalculator feeCalculator; //@5

    public PaymentService(DataSource dataSource, InvoiceDao invoiceDao, LedgerDao ledgerDao, AccountDao accountDao) { //@1-5
    public PaymentService(DataSource dataSource, InvoiceDao invoiceDao, LedgerDao ledgerDao, AccountDao accountDao, //@5
            FeeCalculator feeCalculator) { //@5
        this.dataSource = dataSource;
        this.invoiceDao = invoiceDao;
        this.ledgerDao = ledgerDao;
        this.accountDao = accountDao;
        this.feeCalculator = feeCalculator; //@5
    }

    public PaymentReceipt applyPayment(long invoiceId, BigDecimal amount) throws SQLException { //@1-10
    public PaymentReceipt applyPayment(long invoiceId, BigDecimal amount, String reference) throws SQLException { //@10
        Invoice invoice = invoiceDao.findById(invoiceId);
        if (invoice == null) {
            throw new IllegalArgumentException("unknown invoice " + invoiceId);
        }
        // Gateway callbacks can arrive twice; posting the same reference again //@10
        // double-credits the account. //@10
        if (ledgerDao.existsByReference(reference)) { //@10
            return PaymentReceipt.duplicate(invoiceId, reference); //@10
        } //@10
        BigDecimal adjusted = Tax2013Adjuster.adjust(amount, invoice.getIssuedOn()); //@3-9
        BigDecimal fee = feeCalculator.lateFee(invoice); //@5
        BigDecimal total = adjusted; //@3-5
        BigDecimal total = adjusted.subtract(fee); //@5-9
        BigDecimal total = amount.subtract(fee); //@9
        accountDao.debit(invoice.getAccountId(), amount); //@1-3
        ledgerDao.insertEntry(invoice.getAccountId(), invoiceId, amount, "PAYMENT"); //@1-3
        accountDao.debit(invoice.getAccountId(), total); //@3-4
        ledgerDao.insertEntry(invoice.getAccountId(), invoiceId, total, "PAYMENT"); //@3-4
        postThroughProcedure(invoice.getAccountId(), invoiceId, total); //@4-10
        postThroughProcedure(invoice.getAccountId(), invoiceId, total, reference); //@10
        if (fee.signum() > 0) { //@5
            ledgerDao.insertEntry(invoice.getAccountId(), invoiceId, fee, "LATE_FEE"); //@5-10
            ledgerDao.insertEntry(invoice.getAccountId(), invoiceId, fee, "LATE_FEE", reference); //@10
        } //@5
        invoiceDao.markPaid(invoiceId);
        return new PaymentReceipt(invoiceId, amount); //@1-5
        return new PaymentReceipt(invoiceId, total, fee); //@5
    }

    /** //@4
     * The balance update and the ledger insert must happen in one database //@4
     * transaction; doing it from Java lost updates under concurrent payments. //@4
     */ //@4
    private void postThroughProcedure(long accountId, long invoiceId, BigDecimal amount) throws SQLException { //@4-10
    private void postThroughProcedure(long accountId, long invoiceId, BigDecimal amount, String reference) //@10
            throws SQLException { //@10
        try (Connection connection = dataSource.getConnection(); //@4
                CallableStatement call = connection.prepareCall("{call sp_apply_payment(?, ?, ?)}")) { //@4-10
                CallableStatement call = connection.prepareCall("{call sp_apply_payment(?, ?, ?, ?)}")) { //@10
            call.setLong(1, accountId); //@4
            call.setLong(2, invoiceId); //@4
            call.setBigDecimal(3, amount); //@4
            call.setString(4, reference); //@10
            call.execute(); //@4
        } //@4
    } //@4
}
