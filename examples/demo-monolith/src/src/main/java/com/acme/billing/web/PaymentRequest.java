package com.acme.billing.web;

import java.math.BigDecimal;

public class PaymentRequest {

    private BigDecimal amount;
    private String gatewayReference;

    public BigDecimal getAmount() {
        return amount;
    }

    public void setAmount(BigDecimal amount) {
        this.amount = amount;
    }

    public String getGatewayReference() {
        return gatewayReference;
    }

    public void setGatewayReference(String gatewayReference) {
        this.gatewayReference = gatewayReference;
    }
}
