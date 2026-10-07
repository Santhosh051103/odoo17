from odoo import api, fields, models, _, Command
from odoo.exceptions import UserError, ValidationError, AccessError, RedirectWarning
from datetime import timedelta
import pdb
import logging
from odoo.tools import html2plaintext
_logger = logging.getLogger(__name__)

class AccountReimbursementLine(models.Model):
    _name = 'payment.other.charges.lines'
    _description = 'Payment Other Charges'

    payment_id = fields.Many2one('account.payment', string="Payment Reference", readonly=True)
    company_id = fields.Many2one('res.company', string="Company", readonly=True, related='payment_id.company_id')
    account_id = fields.Many2one('account.account', string="Account", company_dependent=True)
    tax_id = fields.Many2one('account.tax',string='Tax')
    tag_ids = fields.Many2many('account.account.tag',string='Tax Grids')
    other_charge = fields.Float('Amount', compute='_compute_other_charge_tax_id', store=True)

    @api.onchange('tax_id')
    @api.depends('tax_id', 'payment_id.other_charges_lines','payment_id.amount', 'payment_id.payment_base_amount')
    def _compute_other_charge_tax_id(self):
        for rec in self:
            if rec.tax_id:
                if rec.tax_id.amount_type == 'percent':
                    rec.other_charge = round(rec.payment_id.payment_base_amount*(rec.tax_id.amount/100))
                    tax_repartition_line = rec.tax_id.invoice_repartition_line_ids.filtered(
                        lambda l: l.account_id and not l.repartition_type == 'base'
                    )[:1]
                    if tax_repartition_line:
                        rec.account_id = tax_repartition_line.account_id
                    else:
                        rec.account_id = False
                else:
                    rec.account_id = False
                    rec.other_charge = 0.0
                other_lines = rec.payment_id.other_charges_lines.filtered(lambda r: r.other_charge != 0)
                # AMOUNT-WRITE-GUARD: write the payment amount only when it really changes. Writing it
                # again with the same value still triggered a re-sync of the journal entry while its
                # lines were being built, which duplicated every line (two bank lines -> Odoo refuses).
                new_amount = rec.payment_id.payment_base_amount + float(sum(other_lines.mapped('other_charge')) or 0)
                if rec.payment_id.amount != new_amount:
                    rec.payment_id.amount = new_amount

class AccountPayment(models.Model):
    _inherit = "account.payment"

    utr_number = fields.Char('UTR Number', copy=False)
    is_utr_updated = fields.Boolean(string='UTR Updated',copy=False)
    old_utr_number = fields.Char('OLD UTR Number', copy=False)
    is_fund_requsiting = fields.Boolean(string='Fund Requisition', copy=False)
    is_contra_payment = fields.Boolean(string='Contra Payment', copy=False)
    is_credit_payment = fields.Boolean(string='Credit Payment', copy=False)
    approval_state = fields.Char(string='Approval Status', compute='compute_approval_state', store=True, copy=False,
                                 tracking=True)
    approval_document = fields.Many2one('multi.approval', string='Approval Record', copy=False)
    reason_approved = fields.Text(string='Approval comments', copy=False)
    partner_cl_balance = fields.Float(compute='_get_partner_cl_balance', string='Partner Balance')
    closing_balance = fields.Float(compute='_get_closing_balance', string='Closing Balance')
    type = fields.Selection([
        ("capex", "Capex"),
        ("opex", "Opex")], default='opex', string="Capex/Opex")
    payment_purchase_id = fields.Many2one('purchase.order',copy=False, string='Purchase Order')
    recurring = fields.Boolean(string='Recurring Payment', copy=False)
    recurring_days = fields.Integer(string='Recurring Days', default="1", copy=False)
    recurring_until_date = fields.Date(string='Recurring Until Date', copy=False)
    is_cheque_details_freeze = fields.Boolean(string='Is Cheque Details Freezed')
    other_charge_applicable = fields.Boolean(string='Other Charges Applicable?')
    payment_base_amount = fields.Float(string="Base Amount")
    other_charges_lines = fields.One2many('payment.other.charges.lines', 'payment_id', string="Other Charges", copy=True)
    print_assigned = fields.Many2many('res.users',string='Print Assigned To')
    print_assigned_true = fields.Boolean(string='Print Assigned True',compute='compute_print_assigned')
    # ref = fields.Text()

    @api.depends('print_assigned')
    def compute_print_assigned(self):
        for rec in self:
            rec.print_assigned_true = False
            if rec.payment_type == 'outbound' and not rec.show_partner_bank_account:
                if rec.state in ['posted', 'approved'] or self.env.user.id in rec.print_assigned.ids:
            # if self.env.user.id in rec.print_assigned.ids:
                    rec.print_assigned_true = True

    # @api.model
    # def _get_trigger_fields_to_synchronize(self):
    #     return (
    #         'date', 'amount', 'payment_type', 'partner_type', 'payment_reference', 'is_internal_transfer',
    #         'currency_id', 'partner_id', 'destination_account_id', 'partner_bank_id', 'journal_id', 'other_charges_lines',
    #         'payment_base_amount','other_charge_applicable'
    #     )

    # @api.onchange('other_charges_lines')
    # def _onchange_other_charges_lines(self):
    #     if self.move_id and self.move_id.state == 'draft':
    #         self.move_id.line_ids.filtered(lambda l: l.other_charges_payment_line).unlink()

    @api.onchange('payment_base_amount')
    @api.depends('other_charges_lines.other_charge','payment_base_amount')
    def _update_payment_amount_base(self):
        for line in self.filtered(lambda l: l.payment_base_amount>0 and l.other_charge_applicable):
            # OTHER-CHARGES-AMOUNT-FIX: read this payment's own charge lines (self.payment_id is empty
            # here, so the TDS was ignored and the Amount came out equal to the Base Amount).
            other_lines = line.other_charges_lines.filtered(lambda r: r.other_charge != 0)
            line.amount = line.payment_base_amount + float(sum(other_lines.mapped('other_charge')) or 0)

    def _prepare_move_line_default_vals(self, write_off_line_vals=None, force_balance=None):
        self.ensure_one()
        # Call super with same signature so parent logic can use inputs if needed
        if not self.other_charges_lines:
            print("fffffffffffffffffffffffffff777777777777777777777777777777777777777")
            self.move_id.line_ids.filtered(lambda l: l.other_charges_payment_line).unlink()
        res = super(AccountPayment, self)._prepare_move_line_default_vals(
            write_off_line_vals=write_off_line_vals, force_balance=force_balance
        )

        if not self.other_charges_lines:
            return res

        if not self.outstanding_account_id:
            raise UserError(_(
                "You can't create a new payment without an outstanding payments/receipts account set "
                "either on the %s payment method in the %s journal."
            ) % (self.payment_method_line_id.name, self.journal_id.display_name))

        write_off_line_vals_list = write_off_line_vals or []
        write_off_amount_currency = sum(x.get('amount_currency', 0.0) for x in write_off_line_vals_list)
        write_off_balance = sum(x.get('balance', 0.0) for x in write_off_line_vals_list)

        payment_base_amount = self.payment_base_amount if self.other_charge_applicable else self.amount
        # payment_base_amount = self.amount
        if self.payment_type == 'inbound':
            liquidity_amount_currency = payment_base_amount
        elif self.payment_type == 'outbound':
            liquidity_amount_currency = -payment_base_amount
        else:
            liquidity_amount_currency = 0.0

        if not write_off_line_vals and force_balance is not None:
            sign = 1 if liquidity_amount_currency > 0 else -1
            liquidity_balance = sign * abs(force_balance)
        else:
            liquidity_balance = liquidity_amount_currency

        counterpart_balance = -liquidity_balance
        counterpart_amount_currency = -liquidity_amount_currency

        liquidity_line_name = ''.join(x[1] for x in self._get_liquidity_aml_display_name_list())
        counterpart_line_name = ''.join(x[1] for x in self._get_counterpart_aml_display_name_list())

        line_vals_list = []
        other_charges_list = []

        for line in self.other_charges_lines.filtered(lambda l: l.account_id and l.payment_id.state == 'draft'):
            # skip zero amounts
            if not line.other_charge:
                continue

            # Determine amount sign based on payment_type
            if self.payment_type == 'inbound':
                other_amount_1 = line.other_charge
            elif self.payment_type == 'outbound':
                other_amount_1 = -line.other_charge
            else:
                other_amount_1 = 0.0

            # Compute balance for this other-charge line (preserve original force_balance behaviour)
            if not write_off_line_vals and force_balance is not None:
                sign = 1 if other_amount_1 > 0 else -1
                other_balance_1 = sign * abs(force_balance)
            else:
                other_balance_1 = other_amount_1

            # decrease counterpart by this other charge
            # counterpart_amount_currency -= other_amount_1
            # counterpart_balance -= other_balance_1

            liquidity_amount_currency -= -other_amount_1
            liquidity_balance -= -other_balance_1
            # Tax Grid Updation
            tax_tags = line.tax_id.invoice_repartition_line_ids.filtered(
                lambda l: l.tag_ids
            ).mapped('tag_ids.id')
            other_charges_vals = {
                'name': liquidity_line_name,
                'date_maturity': self.date,
                'amount_currency': -other_amount_1,
                'debit': -other_balance_1 if other_balance_1 < 0.0 else 0.0,
                'credit': other_balance_1 if other_balance_1 > 0.0 else 0.0,
                'partner_id': self.partner_id.id,
                'account_id': line.account_id.id,
                'other_charges_payment_line': True,
                'tax_tag_ids': [(6, 0, tax_tags)]
            }
            other_charges_list.append(other_charges_vals)

        # Liquidity line (always included)
        liquidity_vals = {
            'name': liquidity_line_name,
            'date_maturity': self.date,
            'amount_currency': liquidity_amount_currency,
            'debit': liquidity_balance if liquidity_balance > 0.0 else 0.0,
            'credit': -liquidity_balance if liquidity_balance < 0.0 else 0.0,
            'partner_id': self.partner_id.id,
            'account_id': self.outstanding_account_id.id,
        }
        line_vals_list.append(liquidity_vals)

        # Receivable / Payable line (adjusted by other charges)
        ar_ap_vals = {
            'name': counterpart_line_name,
            'date_maturity': self.date,
            'amount_currency': counterpart_amount_currency,
            'debit': counterpart_balance if counterpart_balance > 0.0 else 0.0,
            'credit': -counterpart_balance if counterpart_balance < 0.0 else 0.0,
            'partner_id': self.partner_id.id,
            'account_id': self.destination_account_id.id,
        }
        line_vals_list.append(ar_ap_vals)
        # pdb.set_trace()
        # Attach other charge lines (if any)
        if other_charges_list:
            line_vals_list.extend(other_charges_list)
        # _logger.info("Printing the lines vals list", line_vals_list)
        return line_vals_list

    @api.model
    def _get_trigger_fields_to_synchronize(self):
        return (
            'date', 'amount', 'payment_type', 'partner_type', 'payment_reference', 'is_internal_transfer',
            'partner_id', 'partner_bank_id', 'journal_id','analytic_account_id', 'other_charges_lines',
            'payment_base_amount','other_charge_applicable'
        )

    def _synchronize_to_moves(self, changed_fields):
        ''' Update the account.move regarding the modified account.payment.
        :param changed_fields: A list containing all modified fields on account.payment.
        '''
        if self._context.get('skip_account_move_synchronization'):
            return

        if not any(field_name in changed_fields for field_name in self._get_trigger_fields_to_synchronize()):
            return
        self.move_id.line_ids.unlink()

        for pay in self.with_context(skip_account_move_synchronization=True):
            liquidity_lines, counterpart_lines, writeoff_lines = pay._seek_for_lines()

            # Make sure to preserve the write-off amount.
            # This allows to create a new payment with custom 'line_ids'.

            write_off_line_vals = []
            if liquidity_lines and counterpart_lines and writeoff_lines:
                write_off_line_vals.append({
                    'name': writeoff_lines[0].name,
                    'account_id': writeoff_lines[0].account_id.id,
                    'partner_id': writeoff_lines[0].partner_id.id,
                    'amount_currency': sum(writeoff_lines.mapped('amount_currency')),
                    'balance': sum(writeoff_lines.mapped('balance')),
                })

            line_vals_list = pay._prepare_move_line_default_vals(write_off_line_vals=write_off_line_vals)
            # pdb.set_trace()

            line_ids_commands = [
                Command.update(liquidity_lines.id, line_vals_list[0]) if liquidity_lines else Command.create(
                    line_vals_list[0]),
                Command.update(counterpart_lines.id, line_vals_list[1]) if counterpart_lines else Command.create(
                    line_vals_list[1])
            ]

            for line in writeoff_lines:
                line_ids_commands.append((2, line.id))

            for extra_line_vals in line_vals_list[2:]:
                line_ids_commands.append((0, 0, extra_line_vals))

            pay.move_id \
                .with_context(skip_invoice_sync=True) \
                .write({
                'partner_id': pay.partner_id.id,
                'partner_bank_id': pay.partner_bank_id.id,
                'line_ids': line_ids_commands,
            })

    def _synchronize_from_moves(self, changed_fields):
        ''' Update the account.payment regarding its related account.move.
        Also, check both models are still consistent.
        :param changed_fields: A set containing all modified fields on account.move.
        '''
        if self._context.get('skip_account_move_synchronization'):
            return

        for pay in self.with_context(skip_account_move_synchronization=True):

            # After the migration to 14.0, the journal entry could be shared between the account.payment and the
            # account.bank.statement.line. In that case, the synchronization will only be made with the statement line.
            if pay.move_id.statement_line_id:
                continue

            move = pay.move_id
            move_vals_to_write = {}
            payment_vals_to_write = {}

            if 'journal_id' in changed_fields:
                if pay.journal_id.type not in ('bank', 'cash'):
                    raise UserError(_("A payment must always belongs to a bank or cash journal."))

            if 'line_ids' in changed_fields:
                all_lines = move.line_ids
                liquidity_lines, counterpart_lines, writeoff_lines = pay._seek_for_lines()
                if len(liquidity_lines) != 1:
                    raise UserError(_(
                        "Journal Entry %s is not valid. In order to proceed, the journal items must "
                        "include one and only one outstanding payments/receipts account.",
                        move.display_name,
                    ))

                # if any(line.currency_id != all_lines[0].currency_id for line in all_lines):
                #     raise UserError(_(
                #         "Journal Entry %s is not valid. In order to proceed, the journal items must "
                #         "share the same currency.",
                #         move.display_name,
                #     ))

                if any(line.partner_id != all_lines[0].partner_id for line in all_lines):
                    raise UserError(_(
                        "Journal Entry %s is not valid. In order to proceed, the journal items must "
                        "share the same partner.",
                        move.display_name,
                    ))

                if counterpart_lines.account_id.account_type == 'asset_receivable':
                    partner_type = 'customer'
                else:
                    partner_type = 'supplier'

                liquidity_amount = liquidity_lines.amount_currency

                move_vals_to_write.update({
                    'partner_id': liquidity_lines.partner_id.id,
                })
                payment_vals_to_write.update({
                    'amount': abs(liquidity_amount),
                    'partner_type': partner_type,
                    'destination_account_id': counterpart_lines.account_id.id,
                    'partner_id': liquidity_lines.partner_id.id,
                })
                if liquidity_amount > 0.0:
                    payment_vals_to_write.update({'payment_type': 'inbound'})
                elif liquidity_amount < 0.0:
                    payment_vals_to_write.update({'payment_type': 'outbound'})

            move.write(move._cleanup_write_orm_values(move, move_vals_to_write))
            pay.write(move._cleanup_write_orm_values(pay, payment_vals_to_write))


    # def action_update_account_payment_outstanding_payment(self):
    #     ###Update Outstanding Payments
    #     records = self.env['account.payment'].browse(self._context.get('active_ids', False))
    #     for record in records:
    #         # pdb.set_trace()
    #         for line in record.move_id.line_ids.filtered(lambda l: l.account_id.account_type in ['asset_cash']):
    #             domain1 =[('code','=', 100204),('company_id','=', record.journal_id.company_id.id)]
    #             coa_id = self.env['account.account'].sudo().search(domain1, order='id desc', limit=1)
    #             # print("dffrt45555556". record.name)
    #             # pdb.set_trace()
    #             if coa_id:
    #                 print("Case1222222222222222222222222222222222222222")
    #             line.write({'account_id': coa_id.id})



    def print_cheque_format(self):
        return self.env.ref('odoo_print_cheque.print_cheque_payment').report_action(self)

    def action_freeze_cheque_details(self):
        for rec in self:
            rec.is_cheque_details_freeze = True
            rec.is_utr_updated = True

    def send_vendor_mail(self):
        form_view = self.env.ref('mail.email_compose_message_wizard_form')

        ctx = {
            'default_model': 'account.payment',
            'default_res_ids': self.ids,
            'default_template_id': self.env.ref('accounts_extended.mail_template_data_payment_receipt_1').id,
            'default_attachment_ids': [],
            'force_email': True,
        }

        return {
            'name': _('Send By Mail'),
            'type': 'ir.actions.act_window',
            'res_model': 'mail.compose.message',
            'view_mode': 'form',
            'views': [(form_view.id, 'form')],
            'target': 'new',
            'context': ctx
        }
        return self.env.ref('account.account_send_payment_receipt_by_email_action')

    def action_create_recurring_payments(self):
        for rec in self:
            print("rec", rec)
            if rec.recurring and rec.recurring_days > 0 and rec.recurring_until_date and rec.state == 'posted':
                print("if condition")
                current_date = fields.Date.today()
                next_payment_date = rec.date + timedelta(days=rec.recurring_days)
                while next_payment_date <= rec.recurring_until_date:
                    print("while condition")
                    print(next_payment_date, "nexx")
                    if next_payment_date == current_date:
                        print(next_payment_date,"nex11111")
                        new_payment = rec.copy(default={
                            'state': 'draft',
                            'date': next_payment_date,
                            'recurring': True,  # Disable recurring for the copied record
                            'recurring_days': rec.recurring_days,
                            'recurring_until_date': rec.recurring_until_date,
                        })
                        print(new_payment, "new_payment")
                    next_payment_date += timedelta(days=rec.recurring_days)

    @api.model
    def create_batch_payment(self):
        res = super().create_batch_payment()
        print(self,'llllllllll')
        check_numbers = self.mapped('cheque_number')
        if len(set(check_numbers)) == 1:
            same_check_number = check_numbers[0]
        else:
            raise UserError("Selected payments must have the same cheque number.")
        batch_payment_id = self.env['account.batch.payment'].browse(res.get('res_id'))
        print(batch_payment_id,'hqqqq')
        batch_payment_id['cheque_number'] = same_check_number
        return res

    def _get_closing_balance(self):
        closing_balance = 0
        query = """
            select sum(balance) as balance FROM account_move_line aml 
            join account_move am on am.id=aml.move_id 
            where am.state='posted' and aml.account_id=%s
            """
        params = tuple(self.journal_id.default_account_id.ids)
        data_get8 = self.env.cr.execute(query, params)
        lines8 = self.env.cr.dictfetchall()
        if (lines8[0].get('balance') != None):
            closing_balance = lines8[0].get('balance') or 0
        for record in self:
            # pdb.set_trace()
            record.closing_balance = closing_balance

    @api.onchange('partner_id')
    def _get_partner_cl_balance(self):
        partner_cl_balance = 0
        if self.partner_id:
            query = """
                select sum(balance) as balance FROM account_move_line aml 
                join account_move am on am.id=aml.move_id 
                join account_account ac on ac.id=aml.account_id 
                where am.state='posted' and aml.partner_id=%s and ac.account_type in ('asset_receivable', 'liability_payable')
                """
            params = tuple(self.partner_id.ids)
            data_get8 = self.env.cr.execute(query, params)
            lines8 = self.env.cr.dictfetchall()
            if (lines8[0].get('balance') != None):
                partner_cl_balance = lines8[0].get('balance') or 0
            for record in self:
                record.partner_cl_balance = partner_cl_balance
        else:
            self.partner_cl_balance = 0

    def action_update_utr_number(self):
        for rec in self:
            if rec.state == 'posted':
                rec.is_cheque_details_freeze = True


    @api.depends('approval_document.type_id.state', 'approval_document.line_ids.state')
    def compute_approval_state(self):
        for record in self:
            if record.approval_document:
                line_states = record.approval_document.line_ids.mapped('state')
                if all(state == 'Draft' for state in line_states):
                    record.approval_state = 'Waiting For Approval'
                elif 'Waiting for Approval' in line_states:
                    waiting_lines = record.approval_document.line_ids.filtered(
                        lambda l: l.state == 'Waiting for Approval')
                    if waiting_lines:
                        record.approval_state = f"Waiting for {', '.join(waiting_lines.mapped('name'))} Approval"
                elif all(state == 'Approved' for state in line_states):
                    record.approval_state = 'Approved'
                elif 'Refused' in line_states:
                    record.approval_state = 'Rejected'
                elif 'Cancel' in line_states:
                    record.approval_state = 'Cancelled'
            else:
                rec = self.env['multi.approval.type'].sudo().search(
                    [('model_id', '=', 'account.payment'), ('state', '=', 'confirm')], limit=1)
                if rec:
                    record.approval_state = 'To Submit for Approval'
                else:
                    record.approval_state = 'Not Applicable'

    @api.depends('partner_id', 'journal_id', 'destination_journal_id')
    def _compute_is_internal_transfer(self):
        for payment in self:
            if 'is_internal_transfer' in self.env.context:
                if self.env.context['is_internal_transfer']:
                    payment.is_internal_transfer = True
            else:
                payment.is_internal_transfer = payment.partner_id \
                                               and payment.partner_id == payment.journal_id.company_id.partner_id \
                                               and payment.destination_journal_id

    @api.onchange('type')
    def onchange_type(self):
        for rec in self:
            # print('rrrrrr',rec.type,rec.move_id._origin.id)
            move = self.env['account.move'].sudo().search([('id', '=', rec.move_id._origin.id)])
            move.expense_type = rec.type

    def action_post(self):
        for pay in self:
            if pay.state != 'approved' and pay.payment_type == 'outbound':
                raise ValidationError('You cannot confirm payments that are not Approved.')
            # if pay.payment_method_line_id.name == 'Cheque' and not pay.is_cheque_cleared and pay.payment_type == 'outbound':
            #     raise UserError(_("Alert !! Kindly Clear the cheque"))
            # if not pay.utr_number and (pay.payment_type == 'outbound' or pay.is_fund_requsiting):
            #     raise UserError(_("Alert !! Kindly update the UTR Number."))
            if pay.amount <= 0:
                raise UserError(_("Alert !! Amount should be greated than Zero"))
            if pay.move_id and pay.utr_number:
                for line in pay.move_id.line_ids:
                    if line.account_id == pay.outstanding_account_id:
                        line.name += ('-' + pay.utr_number)
                pay.old_utr_number = pay.utr_number
            user_email = pay.expense_sheet_id.user_id.email if pay.expense_sheet_id.user_id else ''
            employee_email = pay.expense_sheet_id.employee_id.work_email if pay.expense_sheet_id.employee_id else ''
            # template = self.env.ref('account.mail_template_data_payment_receipt')
            # template.write({'email_to': ', '.join(filter(None, [user_email, employee_email]))})
            # template.send_mail(pay.id, force_send=True)
        res = super(AccountPayment, self).action_post()
        return res

    def action_approve_payment(self):
        for rec in self:
            rec.write({'state': 'approved'})

    def action_reject_payment(self):
        for rec in self:
            rec.write({'state': 'cancel'})

    # -----------------------------------
    # Prepare Ref from Invoice Lines
    # -----------------------------------
    def _prepare_ref_from_invoices(self):
        self.ensure_one()
        values = []

        for line in self.payment_invoice_ids:
            if line.reconcile_amount != 0 and line.invoice_id and line.invoice_id.move_id:
                move = line.invoice_id.move_id

                name = move.name or ''
                approval_id = self.approval_id.name if self.approval_id else ''
                narration = html2plaintext(move.narration or '').replace('\n', ' ').strip()

                combined = f"{approval_id}-{name} - {narration}" if narration else name
                values.append(combined)

        return '\n'.join(values) if values else False

    # -----------------------------------
    # Create
    # -----------------------------------
    @api.model
    def create(self, vals):
        rec = super().create(vals)

        ref_value = rec._prepare_ref_from_invoices()

        update_vals = {'ref': ref_value}

        # CONDITION → update towards also
        if (not rec.show_partner_bank_account) or rec.payment_type == 'inbound':
            update_vals['towards'] = ref_value

        # avoid recursion
        super(type(rec), rec).write(update_vals)

        return rec

    # -----------------------------------
    # Write
    # -----------------------------------
    def write(self, vals):
        res = super().write(vals)

        trigger_fields = {
            'payment_invoice_ids',
            'show_partner_bank_account',
            'payment_type',
            'payment_method_line_id',
            'approval_id'
        }

        if trigger_fields.intersection(vals):
            for rec in self:
                ref_value = rec._prepare_ref_from_invoices()

                update_vals = {'ref': ref_value}

                if (not rec.show_partner_bank_account) or rec.payment_type == 'inbound':
                    if rec.towards != ref_value:
                        update_vals['towards'] = ref_value

                # single safe write (no recursion)
                super(type(rec), rec).write(update_vals)

        return res

class AccountBatchPayment(models.Model):
    _inherit = 'account.batch.payment'
    def action_print_bank_advice_payment_pdf(self):
        return self.env.ref('account_batch_payment.action_print_batch_payment').report_action(self)
