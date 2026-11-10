package com.acme.billing.web;

import com.acme.billing.dao.AccountDao;
import com.acme.billing.model.Account;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/accounts")
public class AccountController {

    private final AccountDao accountDao;

    public AccountController(AccountDao accountDao) {
        this.accountDao = accountDao;
    }

    @GetMapping("/{id}/balance")
    public Account balance(@PathVariable long id) {
        return accountDao.findById(id);
    }
}
