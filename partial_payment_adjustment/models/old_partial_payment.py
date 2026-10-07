from odoo import _, api, fields, models , Command
from odoo.exceptions import UserError
from datetime import date, timedelta

class AccountPaymentInvoices(models.Model):
    _name = 'account.payment.invoice.line'
    _description = "Account Payment Invoices"

    invoice_id = fields.Many2one('account.move.line', string='Invoice')
    date = fields.Date(string='Date', related='invoice_id.date', store=True)
    payment_id = fields.Many2one('account.payment', string='Payment')
    currency_id = fields.Many2one(related='invoice_id.currency_id',tracking=True)
    reconcile_amount = fields.Monetary(string='Reconcile Amount',tracking=True)
    amount_total = fields.Monetary(string="Amount Total", related='invoice_id.move_id.amount_total',tracking=True)
    residual = fields.Monetary(string="Residual Amount", related='invoice_id.amount_residual_currency',tracking=True)
    is_reconcile_amount = fields.Boolean(string='Reconcile Amount')
    is_reconciled_en = fields.Boolean(string="Reconciled")

    # @api.onchange('reconcile_amount')
    # def reconcile_amount_lines_update(self):
    #     total_amount = 0
    #     pay_id = self.payment_id
    #     for rec in self.payment_id.payment_invoice_ids:
    #         total_amount += rec.reconcile_amount
    #     print(pay_id,total_amount,'bbbbb')
    #     pay_id.amount = total_amount


    # @api.onchange('reconcile_amount')
    # def reconcile_amount_lines_update(self):
    #     total_amount = 0
    #     if self.payment_id:
    #         # for rec in self.payment_id.payment_invoice_ids:
    #         #     total_amount += rec.reconcile_amount
    #         self.payment_id.amount = sum(self.payment_id.payment_invoice_ids.mapped('reconcile_amount'))
    #         self._origin.payment_id.write({
    #             'amount':sum(self.payment_id.payment_invoice_ids.mapped('reconcile_amount'))
    #         })
    #         print("Updated amount:", self.payment_id.amount)


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    payment_invoice_ids = fields.One2many('account.payment.invoice.line', 'payment_id', string="Customer Invoices")
    unallocated_amount = fields.Monetary(string='Residual Amount')
    amount_partial_total = fields.Monetary(string='Advance Amount')
    is_advance_payment = fields.Boolean(string='Is Advance Payment')
    advance_payment = fields.Boolean(string='Advance Payment?')
    is_manual_payment = fields.Boolean(string='Is Manual Payment')
    source_payment = fields.Many2one('account.payment',string='Source Payment')
    advance_payment_done = fields.Boolean(string='Advance Payment Done')
    payment_compute = fields.Float(compute = 'compute_bill_payment_amount',string='Compute')

    # @api.onchange('payment_invoice_ids')
    # def reconcile_amount_lines_update(self):
    #     for rec1 in self:
    #         rec1.amount = sum(rec1.payment_invoice_ids.mapped('reconcile_amount'))

    # @api.depends('reconciled_bill_ids')
    def compute_bill_payment_amount(self):
            self.payment_compute = 0
            if self.reconciled_bill_ids:
                for move in self.reconciled_bill_ids:
                    total_payment = 0
                    if move.state == "posted" and move.is_invoice(include_receipts=True):
                        reconciled_partials = move.sudo()._get_all_reconciled_invoice_partials()
                        for reconciled_partial in reconciled_partials:
                            counterpart_line = reconciled_partial["aml"]
                            payment_id = counterpart_line.payment_id.id

                            # add amount only if payment not already counted
                            if payment_id == self.id:
                                total_payment += reconciled_partial["amount"]
                        move.payment_amount = total_payment
            elif self.reconciled_invoice_ids:
                for move in self.reconciled_invoice_ids:
                    total_payment = 0
                    if move.state == "posted" and move.is_invoice(include_receipts=True):
                        reconciled_partials = move.sudo()._get_all_reconciled_invoice_partials()
                        for reconciled_partial in reconciled_partials:
                            counterpart_line = reconciled_partial["aml"]
                            payment_id = counterpart_line.payment_id.id

                            # add amount only if payment not already counted
                            if payment_id == self.id:
                                total_payment += reconciled_partial["amount"]
                        move.payment_amount = total_payment

    @api.onchange('payment_invoice_ids','amount','amount_partial_total')
    def compute_unallocated_amount(self):
        for rec in self:
            total = 0
            if rec.payment_invoice_ids:
                total = sum(abs(line.reconcile_amount) for line in rec.payment_invoice_ids)
                rec.unallocated_amount = rec.amount_partial_total - rec.amount

    def update_reconcile_amount(self):
        for rec in self:
            invoice_lines = rec.payment_invoice_ids.filtered(lambda l: l.is_reconcile_amount)
            total_reconcile = sum(abs(line.residual) for line in invoice_lines)
            rec.amount = total_reconcile
            rec.amount_onchange_manual()
            rec.compute_unallocated_amount()

            # ✅ compute ref AFTER updating all lines
            ref_value = rec._prepare_ref_from_invoices()

            update_vals = {'ref': ref_value}

            # CONDITION → update towards also
            if (not rec.show_partner_bank_account) or rec.payment_type == 'inbound':
                update_vals['towards'] = ref_value

            # ✅ single write per payment
            super(type(rec), rec).write(update_vals)

    def update_entry(self):
        for rec in self:
            rec.update_to_get_vendor_invoices()

    def amount_onchange_manual(self):
        reconcile_ids = self.payment_invoice_ids.filtered(lambda l:l.is_reconcile_amount).ids
        for rec in self.payment_invoice_ids:
            if rec.id in reconcile_ids:
                rec.write({
                    'reconcile_amount':abs(rec.residual),
                    'is_reconcile_amount':False
                })
            else:
                rec.write({
                    'reconcile_amount': 0,
                    'is_reconcile_amount': False
                })

    def action_payments_advances(self):
        source = self.env['account.payment'].sudo().search([('source_payment', '=', self.id)])
        return {
            'type': 'ir.actions.act_window',
            'name': 'Reference Payments',
            'view_mode': 'tree,form',
            'res_model': 'account.payment',
            'domain': [('id', 'in', source.ids)],
        }

    def reconcile_entry(self):
        for payment in self:
            for line_id in payment.payment_invoice_ids.filtered(lambda line: line.reconcile_amount > 0):
                if not line_id.reconcile_amount:
                    continue
                if payment.payment_type == 'outbound':
                    lines = payment.move_id.line_ids.filtered(
                        lambda line: line.debit > 0 and line.account_id.account_type in ['asset_receivable',
                                                                                         'liability_payable'])
                    lines += line_id.invoice_id.move_id.line_ids.filtered(
                        lambda line: line.account_id == lines[0].account_id and not line.reconciled)
                    lines.with_context(amount=line_id.reconcile_amount).reconcile()
                    lines.is_entry_reconciled = True

                elif payment.payment_type == 'inbound':
                    lines = payment.move_id.line_ids.filtered(
                        lambda line: line.credit > 0 and line.account_id.account_type in ['asset_receivable',
                                                                                          'liability_payable'])
                    # pdb.set_trace()
                    lines += line_id.invoice_id.move_id.line_ids.filtered(
                        lambda line: line.account_id == lines[0].account_id and not line.reconciled)
                    lines.with_context(amount=line_id.reconcile_amount).reconcile()
                    lines.is_entry_reconciled = True

    def update_to_get_vendor_invoices(self):
        if self.payment_type in ['outbound'] and self.partner_type and self.partner_id and self.currency_id:
            self.payment_invoice_ids = [(6, 0, [])]
            domain1= [
                ('partner_id', 'child_of', self.partner_id.id),
                ('move_id.state', '=', 'posted'),
                ('move_id.move_type', 'in', ['entry', 'in_invoice', 'in_refund']),
                ('account_id.account_type', 'in', ['liability_payable']),
                ('amount_residual', '!=', 0),('credit', '!=', 0),('company_id', '=', self.company_id.id),
                ('currency_id', '=', self.currency_id.id)]
            invoice_recs = self.env['account.move.line'].sudo().search(domain1)
            invoice_recs = invoice_recs.sorted(lambda l: l.move_id.invoice_date)
            payment_invoice_values = []
            for invoice_rec in invoice_recs:
                payment_invoice_values.append([0, 0, {'invoice_id': invoice_rec.id}])
            self.payment_invoice_ids = payment_invoice_values
        if self.payment_type in ['inbound'] and self.partner_type and self.partner_id and self.currency_id:
            self.payment_invoice_ids = [(6, 0, [])]
            domain1= [
                ('partner_id', 'child_of', self.partner_id.id),
                ('move_id.state', '=', 'posted'),
                ('move_id.move_type', 'in', ['out_invoice', 'out_refund']),
                ('account_id.account_type', 'in', ['asset_receivable']),
                ('amount_residual', '!=', 0),('debit', '!=', 0),('company_id', '=', self.company_id.id),
                ('currency_id', '=', self.currency_id.id)]
            invoice_recs = self.env['account.move.line'].sudo().search(domain1)
            payment_invoice_values = []
            for invoice_rec in invoice_recs:
                payment_invoice_values.append([0, 0, {'invoice_id': invoice_rec.id}])
            self.payment_invoice_ids = payment_invoice_values

    @api.onchange('payment_type', 'partner_type', 'partner_id', 'currency_id')
    def _onchange_to_get_vendor_invoices(self):
        if self.payment_type in ['outbound'] and self.partner_type and self.partner_id and self.currency_id:
            self.payment_invoice_ids = [(6, 0, [])]
            domain1= [
                ('partner_id', 'child_of', self.partner_id.id),
                ('move_id.state', '=', 'posted'),
                ('move_id.move_type', 'in', ['in_invoice', 'in_refund']),
                ('account_id.account_type', 'in', ['liability_payable']),
                ('amount_residual', '!=', 0),('credit', '!=', 0),('company_id', '=', self.company_id.id),
                ('currency_id', '=', self.currency_id.id)]
            invoice_recs = self.env['account.move.line'].sudo().search(domain1)
            invoice_recs = invoice_recs.sorted(lambda l: l.move_id.invoice_date)
            payment_invoice_values = []
            for invoice_rec in invoice_recs:
                payment_invoice_values.append([0, 0, {'invoice_id': invoice_rec.id}])
            self.payment_invoice_ids = payment_invoice_values
            # for line in invoice_recs:
            #     if line.id == 35443:
            #         pdb.set_trace()
            # payment_invoice_values = []
            # for invoice_rec in invoice_recs:
            #     move_type = invoice_rec.move_id.move_type
            #     if move_type != 'entry':
            #         total = invoice_rec.move_id.amount_total
            #     else:
            #         total = invoice_rec.debit if invoice_rec.debit > 0 else invoice_rec.credit

            #     payment_invoice_values.append([0, 0, {
            #         # 'invoice_id': invoice_rec.id,
            #         'amount_total': total,
            #     }])
            # self.payment_invoice_ids = invoice_recs
        if self.payment_type in ['inbound'] and self.partner_type and self.partner_id and self.currency_id:
            self.payment_invoice_ids = [(6, 0, [])]
            domain1= [
                ('partner_id', 'child_of', self.partner_id.id),
                ('move_id.state', '=', 'posted'),
                ('move_id.move_type', 'in', ['entry', 'out_invoice', 'out_refund']),
                ('account_id.account_type', 'in', ['asset_receivable']),
                ('amount_residual', '!=', 0),('debit', '!=', 0),('company_id', '=', self.company_id.id),
                ('currency_id', '=', self.currency_id.id)]
            invoice_recs = self.env['account.move.line'].sudo().search(domain1)
            invoice_recs = invoice_recs.sorted(
                lambda l: l.move_id.invoice_date or date.max
            )
            # pdb.set_trace()
            # payment_invoice_values = []
            # for invoice_rec in invoice_recs:
            #     move_type = invoice_rec.move_id.move_type
            #     if move_type != 'entry':
            #         total = invoice_rec.move_id.amount_total
            #     else:
            #         total = invoice_rec.debit if invoice_rec.debit > 0 else invoice_rec.credit

            #     payment_invoice_values.append([0, 0, {
            #         'invoice_id': invoice_rec.id,
            #         'amount_total': total,
            #     }])
            # self.payment_invoice_ids = invoice_recs
            payment_invoice_values = []
            for invoice_rec in invoice_recs:
                payment_invoice_values.append([0, 0, {'invoice_id': invoice_rec.id}])
            self.payment_invoice_ids = payment_invoice_values

    # @api.onchange('payment_invoice_ids')
    # def reconcile_amount_onchange(self):
    #     for rec in self:
    #         total = 0
    #         for pay in rec.payment_invoice_ids:
    #             total += pay.reconcile_amount
    #         if rec.amount < total:
    #             raise UserError(
    #                 _("Alert!! You are trying to allocate an amount that exceeds the payment amount."))

    @api.onchange('advance_payment')
    def amount_advance_payment(self):
        if self.advance_payment:
            self.payment_invoice_ids.update({'reconcile_amount':0.0})


    @api.onchange('amount')
    def amount_onchange(self):
        for rec in self:
            if not rec.advance_payment:
                if rec.payment_invoice_ids:
                    for line in rec.payment_invoice_ids:
                        print('444')
                        # line.reconcile_amount = 0.0
                    if rec.amount > 0:
                        for line in rec.payment_invoice_ids:
                            total_reconcile = sum(abs(line.reconcile_amount) for line in rec.payment_invoice_ids)
                            if total_reconcile < rec.amount:
                                available_amount = rec.amount - total_reconcile
                                if abs(line.residual) < available_amount:
                                        print('1')
                                        line.reconcile_amount = abs(line.residual)
                                else:
                                    print('2')
                                    line.reconcile_amount = available_amount
                    else:
                        for line in rec.payment_invoice_ids:
                            print('3')
                            line.reconcile_amount = 0

    # def write(self,vals):
    #     super(AccountPayment,self).write(vals)
    #     total_amount = 0
    #     for rec1 in self:
    #         for rec in rec1.payment_invoice_ids:
    #             total_amount += rec.reconcile_amount
    #         print('total---------',total_amount)
    #         rec1.amount = total_amount



    def action_draft(self):
        super(AccountPayment, self).action_draft()
        for payment in self:
            if payment.payment_invoice_ids:
                if payment.is_advance_payment:
                    total_reconcile_amount = sum(payment.payment_invoice_ids.mapped('reconcile_amount'))
                    payment.unallocated_amount += total_reconcile_amount
                if not payment.is_advance_payment and payment.source_payment:
                    total_reconcile_amount = sum(payment.payment_invoice_ids.mapped('reconcile_amount'))
                    payment.source_payment.unallocated_amount += total_reconcile_amount

    def action_post(self):
        super(AccountPayment, self).action_post()
        for payment in self:
            if payment.payment_invoice_ids:
                total_reconcile_amount = sum(payment.payment_invoice_ids.mapped('reconcile_amount'))
                # total_payment_available = sum(payment.other_charges_lines.mapped('other_charge')) + payment.amount
                total_payment_amount_main = payment.amount
                # total_reconcile_line_amount = sum(payment.other_charges_lines.mapped('other_charge'))
                total_reconcile_line_amount = 0
                total_payment_available = total_payment_amount_main + total_reconcile_line_amount
                # pdb.set_trace()
                # if total_payment_available != total_reconcile_amount:
                #     raise UserError(
                #         _("The sum of the reconcile amount of listed invoices is not equal to payment amount."))
                if not self.advance_payment and not payment.payment_invoice_ids.filtered(lambda line: line.reconcile_amount > 0):
                    raise UserError(_("Kindly update the amount to reconcile for each transactions."))

            if payment.payment_invoice_ids.filtered(lambda line: line.reconcile_amount <= 0):
                payment.payment_invoice_ids.filtered(lambda line: line.reconcile_amount <= 0).sudo().unlink()
            for line_id in payment.payment_invoice_ids.filtered(lambda line: line.reconcile_amount > 0):
                if not line_id.reconcile_amount:
                    continue
                if line_id.amount_total <= line_id.reconcile_amount:
                    self.ensure_one()
                    if payment.payment_type == 'inbound':
                        lines = payment.move_id.line_ids.filtered(lambda line: line.credit > 0 and line.account_id.account_type in ['asset_receivable','liability_payable'])
                        # pdb.set_trace()
                        lines += line_id.invoice_id.move_id.line_ids.filtered(
                            lambda line: line.account_id == lines[0].account_id and not line.reconciled)
                        lines.reconcile()
                        lines.is_entry_reconciled = True
                    elif payment.payment_type == 'outbound':
                        lines = payment.move_id.line_ids.filtered(lambda line: line.debit > 0 and line.account_id.account_type in ['asset_receivable','liability_payable'])
                        lines += line_id.invoice_id.move_id.line_ids.filtered(
                            lambda line: line.account_id == lines[0].account_id and not line.reconciled)
                        lines.reconcile()
                        lines.is_entry_reconciled = True
                else:
                    self.ensure_one()
                    # pdb.set_trace()
                    if payment.payment_type == 'inbound':
                        lines = payment.move_id.line_ids.filtered(lambda line: line.credit > 0 and line.account_id.account_type in ['asset_receivable','liability_payable'])
                        if lines:
                            if line_id.invoice_id.move_id.filtered(lambda line: line.move_type != 'entry'):
                                lines = payment.move_id.line_ids.filtered(lambda line: line.credit > 0 and line.account_id.account_type in ['asset_receivable','liability_payable'])
                                lines += line_id.invoice_id.move_id.line_ids.filtered(
                                    lambda line: line.account_id == lines[0].account_id and not line.reconciled)
                                lines.with_context(amount=-line_id.reconcile_amount).reconcile()
                            if line_id.invoice_id.move_id.filtered(lambda line: line.move_type == 'entry'):
                                lines = payment.move_id.line_ids.filtered(lambda line: line.credit > 0 and line.account_id.account_type in ['asset_receivable','liability_payable'])
                                for m in range(len(lines)):
                                    my_list = []
                                    if not line_id.id in my_list:
                                        sasi1111= line_id.filtered(
                                            lambda l: l.reconcile_amount > 0)
                                        direct_je_line = sasi1111.invoice_id.move_id.line_ids.filtered(
                                            lambda line: line.account_id.id == line_id.invoice_id.account_id.id and not line.reconciled)
                                        if len(direct_je_line) <=1:
                                            lines += direct_je_line
                                            lines.with_context(amount=-line_id.reconcile_amount).reconcile()
                                            lines.is_entry_reconciled = True
                                        elif len(direct_je_line) >1:
                                            my_list2 = []
                                            for i in range(len(direct_je_line)):
                                                if line_id.id not in my_list2:
                                                    lines += line_id.invoice_id
                                                    lines.with_context(amount=-line_id.reconcile_amount).reconcile()
                                                    lines.is_entry_reconciled = True
                                                my_list2.append(line_id.id)
                                    my_list.append(line_id.id)
                    elif payment.payment_type == 'outbound':
                        if line_id.invoice_id.move_id.filtered(lambda line: line.move_type != 'entry'):
                            lines = payment.move_id.line_ids.filtered(lambda line: line.debit > 0 and line.account_id.account_type in ['asset_receivable','liability_payable'])
                            if lines:
                                lines += line_id.invoice_id.move_id.line_ids.filtered(
                                    lambda line: line.account_id == lines[0].account_id and not line.reconciled)
                                lines.with_context(amount=line_id.reconcile_amount).reconcile()
                        elif line_id.invoice_id.move_id.filtered(lambda line: line.move_type == 'entry'):
                            lines = payment.move_id.line_ids.filtered(lambda line: line.debit > 0 and line.account_id.account_type in ['asset_receivable','liability_payable'])
                            for m in range(len(lines)):
                                my_list = []
                                if not line_id.id in my_list:
                                    sasi1111= line_id.filtered(
                                            lambda l: l.reconcile_amount > 0)
                                    direct_je_line = sasi1111.invoice_id.move_id.line_ids.filtered(
                                        lambda line: line.account_id.id == line_id.invoice_id.account_id.id and not line.reconciled)
                                    if len(direct_je_line) <=1:
                                        lines += direct_je_line
                                        lines.with_context(amount=line_id.reconcile_amount).reconcile()
                                        lines.is_entry_reconciled = True
                                    elif len(direct_je_line) >1:
                                        my_list2 = []
                                        for i in range(len(direct_je_line)):
                                            if line_id.id not in my_list2:
                                                lines += line_id.invoice_id
                                                lines.with_context(amount=line_id.reconcile_amount).reconcile()
                                                lines.is_entry_reconciled = True
                                            my_list2.append(line_id.id)
                                my_list.append(line_id.id)
            if not self.is_advance_payment:
                    if self.source_payment:
                        total = self.source_payment.unallocated_amount - self.amount
                        self.source_payment.write({
                            'unallocated_amount':total
                        })
                        if self.source_payment.unallocated_amount <=0:
                            self.source_payment.advance_payment_done = True

            # stop

    # def action_cancel(self):
    #     for rec in self:
    #         res = super().action_cancel()
    #         if rec.unallocated_amount:

