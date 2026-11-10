package com.acme.billing.web;

import com.acme.billing.model.Invoice;
import com.acme.billing.service.PaymentService;
import com.acme.billing.dao.InvoiceDao;
import java.math.BigDecimal;
import org.springframework.web.bind.annotation.*;

/**
 * HTTP entry points for invoices. Called by the customer portal and by the
 * payment gateway callback.
 */
@RestController
@RequestMapping("/invoices")
public class InvoiceController {

    private final PaymentService paymentService;
    private final InvoiceDao invoiceDao;

    public InvoiceController(PaymentService paymentService, InvoiceDao invoiceDao) {
        this.paymentService = paymentService;
        this.invoiceDao = invoiceDao;
    }

    @GetMapping("/{id}")
    public Invoice get(@PathVariable long id) {
        return invoiceDao.findById(id);
    }

    /** Applies a payment. The gateway may deliver the same callback more than once. */
    @PostMapping("/{id}/pay")
    public PaymentReceipt pay(@PathVariable long id, @RequestBody PaymentRequest request) {
        BigDecimal amount = request.getAmount();
        String reference = request.getGatewayReference(); //@10
        return paymentService.applyPayment(id, amount); //@1-10
        return paymentService.applyPayment(id, amount, reference); //@10
    }
}
