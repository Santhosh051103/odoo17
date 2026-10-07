from odoo import models, fields, api, _
from odoo.exceptions import UserError

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

            # Guard: "Update Amount" must not silently zero out the payment
            # when nothing has actually been selected. Previously, with no
            # line ticked, this set rec.amount = 0 without complaint, and
            # that zero-amount payment could then proceed straight into
            # the approval workflow (Draft -> To Approve) with no real
            # value behind it. Require either an advance-payment flag or
            # at least one ticked invoice line before allowing this to run.
            if not rec.advance_payment and not invoice_lines:
                raise UserError(_(
                    "Please tick at least one invoice to reconcile against "
                    "before clicking Update Amount, or mark this as an "
                    "Advance Payment if it isn't meant to settle a "
                    "specific bill."
                ))

            total_reconcile = sum(abs(line.residual) for line in invoice_lines)
            rec.amount = total_reconcile
            rec.amount_onchange_manual()
            rec.compute_unallocated_amount()

            ref_value = rec._prepare_ref_from_invoices()

            update_vals = {'ref': ref_value}

            if (not rec.show_partner_bank_account) or rec.payment_type == 'inbound':
                update_vals['towards'] = ref_value

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
                    lines += line_id.invoice_id.move_id.line_ids.filtered(
                        lambda line: line.account_id == lines[0].account_id and not line.reconciled)
                    lines.with_context(amount=line_id.reconcile_amount).reconcile()
                    lines.is_entry_reconciled = True

    def update_to_get_vendor_invoices(self):
        # SCOPING FIX: when this payment is linked to a bill.approval
        # (approval_id set), only offer bills belonging to that SAME
        # approval -- not every open bill for this vendor across every
        # approval. Without this, it's dangerously easy to tick/type a
        # reconcile amount against the wrong approval's bill (confirmed
        # to have actually happened -- a payment meant for one approval
        # silently reconciled against two OTHER already-settled bills
        # from different approvals, double-paying them). Falls back to
        # the original vendor-wide domain when approval_id isn't set, so
        # this module still works standalone outside the approval flow.
        approval_scope = (
            [('move_id.approval_id', '=', self.approval_id.id)]
            if self.approval_id else []
        )
        if self.payment_type in ['outbound'] and self.partner_type and self.partner_id and self.currency_id:
            self.payment_invoice_ids = [(6, 0, [])]
            domain1= [
                ('partner_id', 'child_of', self.partner_id.id),
                ('move_id.state', '=', 'posted'),
                ('move_id.move_type', 'in', ['entry', 'in_invoice', 'in_refund']),
                ('account_id.account_type', 'in', ['liability_payable']),
                ('amount_residual', '!=', 0),('credit', '!=', 0),('company_id', '=', self.company_id.id),
                ('currency_id', '=', self.currency_id.id)] + approval_scope
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
                ('currency_id', '=', self.currency_id.id)] + approval_scope
            invoice_recs = self.env['account.move.line'].sudo().search(domain1)
            payment_invoice_values = []
            for invoice_rec in invoice_recs:
                payment_invoice_values.append([0, 0, {'invoice_id': invoice_rec.id}])
            self.payment_invoice_ids = payment_invoice_values

    @api.onchange('payment_type', 'partner_type', 'partner_id', 'currency_id', 'approval_id')
    def _onchange_to_get_vendor_invoices(self):
        # Same scoping fix as update_to_get_vendor_invoices above.
        approval_scope = (
            [('move_id.approval_id', '=', self.approval_id.id)]
            if self.approval_id else []
        )
        if self.payment_type in ['outbound'] and self.partner_type and self.partner_id and self.currency_id:
            self.payment_invoice_ids = [(6, 0, [])]
            domain1= [
                ('partner_id', 'child_of', self.partner_id.id),
                ('move_id.state', '=', 'posted'),
                ('move_id.move_type', 'in', ['in_invoice', 'in_refund']),
                ('account_id.account_type', 'in', ['liability_payable']),
                ('amount_residual', '!=', 0),('credit', '!=', 0),('company_id', '=', self.company_id.id),
                ('currency_id', '=', self.currency_id.id)] + approval_scope
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
                ('move_id.move_type', 'in', ['entry', 'out_invoice', 'out_refund']),
                ('account_id.account_type', 'in', ['asset_receivable']),
                ('amount_residual', '!=', 0),('debit', '!=', 0),('company_id', '=', self.company_id.id),
                ('currency_id', '=', self.currency_id.id)] + approval_scope
            invoice_recs = self.env['account.move.line'].sudo().search(domain1)
            invoice_recs = invoice_recs.sorted(
                lambda l: l.move_id.invoice_date or fields.Date.max
            )
            payment_invoice_values = []
            for invoice_rec in invoice_recs:
                payment_invoice_values.append([0, 0, {'invoice_id': invoice_rec.id}])
            self.payment_invoice_ids = payment_invoice_values

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
                        pass
                    if rec.amount > 0:
                        for line in rec.payment_invoice_ids:
                            total_reconcile = sum(abs(line.reconcile_amount) for line in rec.payment_invoice_ids)
                            if total_reconcile < rec.amount:
                                available_amount = rec.amount - total_reconcile
                                if abs(line.residual) < available_amount:
                                        line.reconcile_amount = abs(line.residual)
                                else:
                                    line.reconcile_amount = available_amount
                    else:
                        for line in rec.payment_invoice_ids:
                            line.reconcile_amount = 0

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
                total_payment_amount_main = payment.amount
                total_reconcile_line_amount = 0
                total_payment_available = total_payment_amount_main + total_reconcile_line_amount
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
