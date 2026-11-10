package com.acme.billing.util;

import org.springframework.jdbc.core.JdbcTemplate;

/**
 * Runtime switches stored in the database so that operations can flip them
 * without a deployment.
 */
public class LegacyFlags {

    private final JdbcTemplate jdbc;

    public LegacyFlags(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public boolean isEnabled(String flag) {
        Integer enabled = jdbc.queryForObject(
                "SELECT enabled FROM tbl_feature_flag WHERE name = ?", Integer.class, flag);
        return enabled != null && enabled == 1;
    }
}
