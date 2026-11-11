package com.acme.billing.service;

import com.acme.billing.dao.LedgerDao;
import java.math.BigDecimal;
import java.sql.CallableStatement;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import javax.sql.DataSource;
import org.springframework.scheduling.annotation.Scheduled;

/**
 * Compares invoice totals with the ledger every night and closes the period
 * on the first day of a month.
 */
public class NightlyReconciliationJob {

    private static final String MISMATCHES =
            "SELECT i.id, i.amount, COALESCE(SUM(l.amount), 0) AS posted "
            + "FROM tbl_invoice i LEFT JOIN tbl_ledger l ON l.invoice_id = i.id "
            + "WHERE i.status = 'PAID' ";

    private final DataSource dataSource;
    private final LedgerDao ledgerDao;

    public NightlyReconciliationJob(DataSource dataSource, LedgerDao ledgerDao) {
        this.dataSource = dataSource;
        this.ledgerDao = ledgerDao;
    }

    @Scheduled(cron = "0 0 2 * * *")
    public void reconcile() throws SQLException {
        String sql = MISMATCHES;
        if (!includeLegalHold()) { //@13
            sql += "AND i.account_id NOT IN (SELECT id FROM tbl_account WHERE legal_hold = 1) "; //@13
        } //@13
        sql += "GROUP BY i.id, i.amount HAVING i.amount <> COALESCE(SUM(l.amount), 0)";
        try (Connection connection = dataSource.getConnection();
                PreparedStatement statement = connection.prepareStatement(sql);
                ResultSet rows = statement.executeQuery()) {
            while (rows.next()) {
                recordException(connection, rows.getLong("id"), rows.getBigDecimal("amount"), rows.getBigDecimal("posted"));
            }
            closePeriodIfDue(connection);
        }
    }

    private boolean includeLegalHold() { //@13
        return Boolean.getBoolean("billing.recon.includeLegalHold"); //@13
    } //@13

    private void recordException(Connection connection, long invoiceId, BigDecimal expected, BigDecimal posted)
            throws SQLException {
        try (PreparedStatement insert = connection.prepareStatement(
                "INSERT INTO tbl_recon_exception (invoice_id, expected, posted, detected_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)")) {
            insert.setLong(1, invoiceId);
            insert.setBigDecimal(2, expected);
            insert.setBigDecimal(3, posted);
            insert.executeUpdate();
        }
    }

    private void closePeriodIfDue(Connection connection) throws SQLException {
        if (java.time.LocalDate.now().getDayOfMonth() != 1) {
            return;
        }
        try (CallableStatement call = connection.prepareCall("{call sp_close_period()}")) {
            call.execute();
        }
    }
}
