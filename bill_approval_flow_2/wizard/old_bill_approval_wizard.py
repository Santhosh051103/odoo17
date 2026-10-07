from odoo import models, fields, api, _
from odoo.exceptions import UserError


class BillApprovalWizard(models.TransientModel):
    _name = 'bill.approval.wizard'
    _description = 'Bill Approval Wizard'

    bill_id      = fields.Many2one('bill.approval', required=True)
    wizard_type  = fields.Selection([
        ('approve_l1', 'Approve L1'),
        ('approve_l2', 'Approve L2'),
        ('approve_l3', 'Approve L3'),
        ('reject',     'Reject'),
        ('bill',       'Create Bill'),
        ('payment',    'Register Payment'),
    ], required=True)

    # Approve / Reject
    comment = fields.Text(string='Comment')

    # Bill
    journal_type = fields.Selection([
        ('purchase', 'Vendor Bill'),
        ('general',  'Journal Entry'),
        ('sale',     'Customer Invoice'),
    ], default='purchase', string='Journal Type')
    partner_id  = fields.Many2one('res.partner', string='Partner')
    account_id  = fields.Many2one(
        'account.account', string='Expense Account',
        domain="[('account_type', '=', 'expense')]",
    )
    bill_note   = fields.Text(string='Notes')

    # Payment
    journal_id  = fields.Many2one(
        'account.journal', string='Payment Journal',
        domain="[('type', 'in', ['bank', 'cash'])]",
    )
    amount       = fields.Float(string='Amount')
    payment_date = fields.Date(string='Payment Date', default=fields.Date.today)
    payment_note = fields.Text(string='Notes')

    @api.onchange('bill_id')
    def _onchange_bill(self):
        if self.bill_id:
            self.amount     = self.bill_id.total_value
            self.partner_id = self.bill_id.supplier_name
            self.account_id = self.bill_id.expense_head

    def action_confirm(self):
        self.ensure_one()
        wt   = self.wizard_type
        bill = self.bill_id

        if wt == 'approve_l1':
            bill._do_approve_l1(self.comment)
        elif wt == 'approve_l2':
            bill._do_approve_l2(self.comment)
        elif wt == 'approve_l3':
            bill._do_approve_l3(self.comment)
        elif wt == 'reject':
            if not self.comment:
                raise UserError(_('Please provide a reason for rejection.'))
            bill._do_reject(self.comment)
        elif wt == 'bill':
            bill._do_create_bill(
                journal_type=self.journal_type,
                partner_id=self.partner_id.id if self.partner_id else False,
                account_id=self.account_id.id if self.account_id else False,
                note=self.bill_note,
            )
        elif wt == 'payment':
            if not self.amount:
                raise UserError(_('Please enter a payment amount.'))
            bill._do_create_payment(
                journal_id=self.journal_id.id if self.journal_id else False,
                amount=self.amount,
                payment_date=self.payment_date,
                note=self.payment_note,
            )

        return {'type': 'ir.actions.act_window_close'}
