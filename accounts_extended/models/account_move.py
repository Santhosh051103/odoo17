# -*- coding: utf-8 -*-
from collections import defaultdict
from contextlib import ExitStack, contextmanager
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
from hashlib import sha256
from json import dumps
import logging
from markupsafe import Markup
from psycopg2 import OperationalError
import re
from textwrap import shorten
from unittest.mock import patch
import base64
from io import BytesIO
from odoo.tools.misc import xlsxwriter
from odoo.tools import html2plaintext, plaintext2html
from num2words import num2words
from odoo import api, fields, models, _, Command
from odoo.addons.base.models.decimal_precision import DecimalPrecision
from odoo.addons.account.tools import format_structured_reference_iso
from odoo.exceptions import UserError, ValidationError, AccessError, RedirectWarning
import io
import xlsxwriter
import re
import calendar
from datetime import datetime
from odoo.tools import (
    date_utils,
    email_re,
    email_split,
    float_compare,
    float_is_zero,
    float_repr,
    format_amount,
    format_date,
    formatLang,
    frozendict,
    get_lang,
    groupby,
    index_exists,
    is_html_empty,
)

_logger = logging.getLogger(__name__)
import pdb
month_field_map = {
                1: 'january_cur_budget',
                2: 'february_cur_budget',
                3: 'march_cur_budget',
                4: 'april_cur_budget',
                5: 'may_cur_budget',
                6: 'june_cur_budget',
                7: 'july_cur_budget',
                8: 'august_cur_budget',
                9: 'september_cur_budget',
                10: 'october_cur_budget',
                11: 'november_cur_budget',
                12: 'december_cur_budget',
            }

MAX_HASH_VERSION = 3

PAYMENT_STATE_SELECTION = [
    ('not_paid', 'Not Paid'),
    ('in_payment', 'In Payment'),
    ('paid', 'Paid'),
    ('partial', 'Partially Paid'),
    ('reversed', 'Reversed'),
    ('invoicing_legacy', 'Invoicing App Legacy'),
]

TYPE_REVERSE_MAP = {
    'entry': 'entry',
    'out_invoice': 'out_refund',
    'out_refund': 'entry',
    'in_invoice': 'in_refund',
    'in_refund': 'entry',
    'out_receipt': 'out_refund',
    'in_receipt': 'in_refund',
}

EMPTY = object()

BILL_APPR = ['in_invoice', 'in_receipt', 'in_refund']


class AccountMoveInherit(models.Model):
    _inherit = 'account.move'

    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('to approve', 'To Approve'),
            ('approved', 'Approved'),
            ('posted', 'Posted'),
            ('cancel', 'Cancelled'),
        ],
        string='Status',
        required=True,
        readonly=True,
        copy=False,
        tracking=True,
        default='draft',
    )
    expense_sequence = fields.Char(string='Task ID')
    expense_type = fields.Selection([
        ("capex", "Capex"),
        ("opex", "Opex")], default='opex', string="Capex/Opex")
    expense_invoice_no = fields.Char(string="Tax/Proforma Invoice No")
    expense_invoice_type_id = fields.Many2one('invoice.type', string="Type of Invoice")
    expense_user_id = fields.Many2one('res.users', string="Requested By", default=lambda self: self.env.user)
    is_payment_approval = fields.Boolean(string="Is Payment Approval", default=False)
    approval_state = fields.Char(string='Approval Status', compute='compute_approval_state', store=True, copy=False,
                                 tracking=True)
    approval_document = fields.Many2one('multi.approval', string='Approval Record', copy=False)
    partner_tcs_warning = fields.Text(
        compute='_compute_partner_tcs_warning',
        groups="account.group_account_invoice,account.group_account_readonly",
    )
    partner_tds_warning = fields.Text(
        compute='_compute_partner_tds_warning',
        groups="account.group_account_invoice,account.group_account_readonly",
    )
    partner_ldc_warning = fields.Text(
        compute='_compute_partner_ldc_warning',
        groups="account.group_account_invoice,account.group_account_readonly",
    )
    budget_id = fields.Many2one('crossovered.budget.lines', 'Budget Code', copy=False, required=0)
    # MIGRATION/FIX NOTE: the previous default here searched for *any*
    # confirmed budget for the company with no date filtering at all (and
    # had a stray `limit=1` mistakenly passed as a kwarg to fields.Many2one
    # instead of to search()), so it could pick an arbitrary/wrong-year
    # budget. Replaced with a plain field; _onchange_date_update_budget()
    # below now selects the budget whose date_from/date_to actually covers
    # this move's Accounting Date.
    crossovered_budget = fields.Many2one('crossovered.budget', string='Budget', copy=False)
    budget_update = fields.Boolean("Is Budget Updated?",copy=False)
    journal_type = fields.Selection(related='journal_id.type')
    active = fields.Boolean(string="Active",default=True, copy=False)
    move_type = fields.Selection(
        selection=[
            ('entry', 'Journal Entry'),
            ('out_invoice', 'Customer Invoice'),
            ('out_refund', 'Customer Credit Note'),
            ('in_invoice', 'Vendor Bill'),
            ('in_refund', 'Vendor Debit Note'),
            ('out_receipt', 'Sales Receipt'),
            ('in_receipt', 'Purchase Receipt'),
        ],
        string='Type',
        required=True,
        readonly=True,
        tracking=True,
        change_default=True,
        index=True,
        default="entry",
    )
    advance_payment_ids = fields.Many2many('account.payment',string='Advance Payment')
    utr_number =fields.Char(string='UTR Number')
    is_cheque_details_freeze = fields.Boolean(string='Is Cheque Details Freezed')

    @api.model_create_multi
    def create(self, vals_list):
        moves = super().create(vals_list)
        # The "TDS Entry" button (l10n_in_withholding) creates a separate
        # withholding journal entry linked back to the original bill via
        # l10n_in_withholding_ref_move_id. That wizard-created move never
        # goes through the UI form, so the _onchange_date_update_budget
        # onchange below never fires for it, leaving Budget blank and
        # forcing a manual pick. Since a TDS entry is always part of the
        # same budget as the bill it's withholding against, inherit it
        # directly from the referenced bill instead of asking.
        for move in moves:
            if (move.l10n_in_withholding_ref_move_id
                    and not move.crossovered_budget
                    and move.l10n_in_withholding_ref_move_id.crossovered_budget):
                move.crossovered_budget = move.l10n_in_withholding_ref_move_id.crossovered_budget
        return moves

    def write(self, vals):
        res = super().write(vals)
        if 'l10n_in_withholding_ref_move_id' in vals:
            for move in self:
                if (move.l10n_in_withholding_ref_move_id
                        and not move.crossovered_budget
                        and move.l10n_in_withholding_ref_move_id.crossovered_budget):
                    move.crossovered_budget = move.l10n_in_withholding_ref_move_id.crossovered_budget
        return res

    def action_update_utr_number(self):
        for rec in self:
            if rec.state == 'posted':
                rec.is_cheque_details_freeze = True


    def send_vendor_mail(self):
        form_view = self.env.ref('mail.email_compose_message_wizard_form')

        ctx = {
            'default_model': 'account.move',
            'default_res_ids': self.ids,
            'default_template_id': self.env.ref('accounts_extended.mail_template_data_journal_payment').id,
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

    def _get_move_display_name(self, show_ref=False):
        ''' Helper to get the display name of an invoice depending of its type.
        :param show_ref:    A flag indicating of the display name must include or not the journal entry reference.
        :return:            A string representing the invoice.
        '''
        self.ensure_one()
        name = ''
        if self.state == 'draft':
            name += {
                'out_invoice': _('Draft Invoice'),
                'out_refund': _('Draft Credit Note'),
                'in_invoice': _('Draft Bill'),
                'in_refund': _('Draft Debit Note'),
                'out_receipt': _('Draft Sales Receipt'),
                'in_receipt': _('Draft Purchase Receipt'),
                'entry': _('Draft Entry'),
            }[self.move_type]
            name += ' '
        if not self.name or self.name == '/':
            if self.id:
                name += '(* %s)' % str(self.id)
        else:
            name += self.name
            if self.env.context.get('input_full_display_name'):
                if self.partner_id:
                    name += f', {self.partner_id.name}'
                if self.date:
                    name += f', {format_date(self.env, self.date)}'
        return name + (f" ({shorten(self.ref, width=50)})" if show_ref and self.ref else '')

    def toggle_active(self):
        # Prevent archiving if the state is not 'cancelled'
        for record in self:
            if record.state not in ('cancel'):
                raise UserError(_("Alert !! You cannot archive a record in the Draft or Posted state."))
        return super(AccountMoveInherit, self).toggle_active()

    @api.depends('company_id', 'invoice_filter_type_domain')
    def _compute_suitable_journal_ids(self):
        for m in self:
            if m.invoice_filter_type_domain:
                journal_type = [m.invoice_filter_type_domain]
            else:
                journal_type = ['cash', 'bank', 'general']
            # pdb.set_trace()
            company = m.company_id or self.env.company
            m.suitable_journal_ids = self.env['account.journal'].search([
                *self.env['account.journal']._check_company_domain(company),
                ('type', 'in', journal_type),
            ])

    @api.depends('partner_id')
    def _compute_partner_ldc_warning(self):
        today = date.today()
        for record in self:
            warning = ''
            if record.partner_id.ldc_expiry_date:
                if record.partner_id.ldc_expiry_date <= today:
                    warning =(f"The LDC expiry date ({record.partner_id.ldc_expiry_date}) for this partner "
                              f"has passed or is effective as of today. Please review and take necessary action.")
            record.partner_ldc_warning = warning

    @api.depends('company_id', 'partner_id', 'amount_total', 'currency_id', 'invoice_line_ids.quantity',
                 'invoice_line_ids.price_unit', 'amount_untaxed_signed')
    def _compute_partner_tcs_warning(self):
        msg = ''
        for move in self.filtered(lambda move: move.partner_id.commercial_partner_id.tcs_applicable):
            move.with_company(move.company_id)
            move.partner_tcs_warning = ''
            invoice_date = move.invoice_date or move.date or fields.Date.today()
            domain1 = [('date_from', '<=', invoice_date), ('date_to', '>=', invoice_date)]
            fiscal_year = self.env['account.fiscal.year'].sudo().search(domain1, limit=1)
            fiscal_year_start_date = fiscal_year_end_date = invoice_date
            basic_amount = 0
            if fiscal_year:
                fiscal_year_start_date = fiscal_year.date_from
                fiscal_year_end_date = fiscal_year.date_to
            if move.partner_id and move.amount_untaxed_signed > 0:
                query1 = """
                        SELECT sum(am.amount_untaxed_signed) as amount_untaxed_signed
                        FROM account_move am
                        where am.move_type in ('out_invoice', 'out_refund', 'out_receipt') and am.partner_id=%s
                        and am.date<=%s and am.date>=%s and am.state='posted';
                        """
                query_params1 = (
                    move.partner_id.commercial_partner_id.id, str(fiscal_year_end_date), str(fiscal_year_start_date))
                data_get1 = self.env.cr.execute(query1, query_params1)
                lines1 = self.env.cr.dictfetchall()
                if lines1[0].get('amount_untaxed_signed') and lines1[0].get('amount_untaxed_signed') != None:
                    basic_amount = lines1[0].get('amount_untaxed_signed')
            show_warning = move.state == 'draft' and move.move_type in ['out_invoice', 'out_receipt']
            if move.move_type in ['out_invoice', 'out_receipt']:
                basic_amount += move.tax_totals['amount_untaxed']
            elif move.move_type in ['out_refund']:
                basic_amount -= move.tax_totals['amount_untaxed']
            tcs_limit = self.partner_id.commercial_partner_id.tcs_applicable or self.company_id.tcs_limit
            tcs_limit_amount = self.partner_id.commercial_partner_id.tcs_limit_amount_partner or self.company_id.tcs_limit_amount
            if show_warning and tcs_limit and basic_amount > float(tcs_limit_amount):
                basic_amount_formatted = formatLang(self.env, basic_amount, currency_obj=move.company_id.currency_id)
                tcs_limit_amount_formatted = formatLang(self.env, float(tcs_limit_amount),
                                                        currency_obj=move.company_id.currency_id)
                msg = "Cummulative Sales for - %s in %s is %s which is exceeding TCS limit of %s." % (
                    move.partner_id.name, fiscal_year.display_name, basic_amount_formatted, tcs_limit_amount_formatted)
        self.partner_tcs_warning = msg

    @api.depends('company_id', 'partner_id', 'amount_total', 'currency_id', 'invoice_line_ids.quantity',
                 'invoice_line_ids.price_unit', 'amount_untaxed_signed')
    def _compute_partner_tds_warning(self):
        msg = ''
        for move in self.filtered(lambda move: move.partner_id.commercial_partner_id.tds_applicable):
            move.with_company(move.company_id)
            move.partner_tds_warning = ''
            invoice_date = move.invoice_date or move.date or fields.Date.today()
            domain1 = [('date_from', '<=', invoice_date), ('date_to', '>=', invoice_date)]
            fiscal_year = self.env['account.fiscal.year'].sudo().search(domain1, limit=1)
            fiscal_year_start_date = fiscal_year_end_date = invoice_date
            basic_amount = 0
            if fiscal_year:
                fiscal_year_start_date = fiscal_year.date_from
                fiscal_year_end_date = fiscal_year.date_to
            if move.partner_id and move.amount_untaxed_signed < 0:
                query1 = """
                           SELECT sum(am.amount_untaxed_signed) as amount_untaxed_signed
                           FROM account_move am
                           where am.move_type in ('in_invoice', 'in_refund', 'in_receipt') and am.partner_id=%s
                           and am.date<=%s and am.date>=%s and am.state='posted';
                           """
                query_params1 = (
                    move.partner_id.commercial_partner_id.id, str(fiscal_year_end_date), str(fiscal_year_start_date))
                data_get1 = self.env.cr.execute(query1, query_params1)
                lines1 = self.env.cr.dictfetchall()
                if lines1[0].get('amount_untaxed_signed') and lines1[0].get('amount_untaxed_signed') != None:
                    basic_amount = -lines1[0].get('amount_untaxed_signed')
            show_warning = move.state == 'draft' and move.move_type in ['in_invoice', 'in_receipt']
            if move.move_type in ['in_invoice', 'in_receipt']:
                basic_amount += move.tax_totals['amount_untaxed']
            elif move.move_type in ['in_refund']:
                basic_amount -= move.tax_totals['amount_untaxed']
            tds_limit = self.partner_id.commercial_partner_id.tds_applicable or self.company_id.tds_limit
            tds_limit_amount = self.partner_id.commercial_partner_id.tds_limit_amount_partner or self.company_id.tds_limit_amount
            tds_tax_id = self.partner_id.commercial_partner_id.tds_tax_id or self.company_id.tds_tax_id
            if show_warning and tds_limit and basic_amount > float(tds_limit_amount):
                basic_amount_formatted = formatLang(self.env, basic_amount, currency_obj=move.company_id.currency_id)
                tds_limit_amount_formatted = formatLang(self.env, float(tds_limit_amount),
                                                        currency_obj=move.company_id.currency_id)
                msg = "Cummulative Purchases for - %s in %s is %s which is exceeding TDS limit of %s. Kindly deduct %s." % (
                    move.partner_id.name, fiscal_year.display_name, basic_amount_formatted, tds_limit_amount_formatted,
                    tds_tax_id.display_name)
        self.partner_tds_warning = msg

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
                entry_rec = self.env['multi.approval.type'].sudo().search(
                    [('model_id', '=', 'account.move'), ('state', '=', 'confirm'),
                     ('description', '=', 'Journal Entries')], limit=1)
                rec = self.env['multi.approval.type'].sudo().search(
                    [('model_id', '=', 'account.move'), ('state', '=', 'confirm')], limit=1)
                if record.move_type == 'entry':
                    if entry_rec:
                        record.approval_state = 'To Submit for Approval'
                    else:
                        record.approval_state = 'Not Applicable'
                elif record.move_type == 'out_invoice':
                    if rec:
                        record.approval_state = 'To Submit for Approval'
                    else:
                        record.approval_state = 'Not Applicable'
                elif record.move_type == 'in_invoice':
                    if rec:
                        record.approval_state = 'To Submit for Approval'

    def button_draft(self):
        super().button_draft()
        for record in self:
            rec = self.env['multi.approval.type'].sudo().search(
                [('model_id', '=', 'account.move'), ('state', '=', 'confirm')], limit=1)
            if rec:
                record.approval_state = 'To Submit for Approval'
                record.x_has_request_approval = False

    def action_approve_payment(self):
        for rec in self:
            rec.write({'state': 'approved'})
            group = self.env.ref('hr_expense_extended.group_post_journal_expense')
            users = group.users
            for rec1 in users:
                self.activity_schedule(
                    activity_type_id=self.env.ref('mail.mail_activity_data_todo').id,
                    summary="Post Journal Reminder: Post Journal reminder",
                    note=f"Journal has been approved.Kindly Post the journal:{self.name}.",
                    user_id=rec1.id,
                    date_deadline=fields.Date.today()
                )

    # def button_cancel(self):
    #     for rec in self:
    #         rec.action_update_budget_cur_figure_minus()
    #     # Shortcut to move from posted to cancelled directly. Useful for E-invoices that must not be changed
    #     # when sent to the government.
    #     moves_to_reset_draft = self.filtered(lambda x: x.state == 'posted')
    #     if moves_to_reset_draft:
    #         moves_to_reset_draft.button_draft()
    #
    #     # Check if any journal entry is neither in 'draft' nor 'approve' state
    #     if any(move.state not in ['draft', 'to approve'] for move in self):
    #         raise UserError(_("Only draft or to approved journal entries can be cancelled."))
    #
    #     # Write state change to 'cancel'
    #     model_name = 'account.move'
    #     res_id = self.id
    #     origin_ref = f"{model_name},{res_id}"
    #     existing_approvals = self.env['multi.approval'].search([("origin_ref", "=", origin_ref)])
    #     existing_approvals.write({'state': 'Cancel'})
    #     self.write({
    #         'auto_post': 'no',
    #         'approval_state': 'Not Applicable',
    #         'x_has_request_approval': False,
    #         'state': 'cancel'
    #     })

    def button_cancel(self):
        for rec in self:
            rec.action_update_budget_cur_figure_minus()
            # Shortcut to move from posted to cancelled directly
            if rec.state == 'posted':
                rec.button_draft()

            # Ensure record is in a valid state
            # if rec.state not in ['draft', 'approved']:
            #     raise UserError(_("Only draft or to approved journal entries can be cancelled."))

            # Update approval status
            model_name = 'account.move'
            res_id = rec.id
            origin_ref = f"{model_name},{res_id}"
            existing_approvals = self.env['multi.approval'].search([("origin_ref", "=", origin_ref)])
            existing_approvals.write({'state': 'Cancel'})

            # Write state change
            rec.write({
                'auto_post': 'no',
                'approval_state': 'Not Applicable',
                'x_has_request_approval': False,
                'state': 'cancel'
            })

    def _find_budget_for_date(self, accounting_date):
        """Return the crossovered.budget whose date_from/date_to range
        covers `accounting_date` for this move's company (e.g. a bill dated
        in 2024 matches "Budget FY 2024-2025", a bill dated in 2026 matches
        "Budget FY 2026-2027", etc.) -- whatever fiscal year ranges your
        budget records are actually configured with."""
        self.ensure_one()
        if not accounting_date:
            return self.env['crossovered.budget']
        return self.env['crossovered.budget'].sudo().search([
            ('date_from', '<=', accounting_date),
            ('date_to', '>=', accounting_date),
            ('user_type', '=', 'odoo'),
            ('company_id', '=', self.company_id.id or self.env.company.id),
            ('state', 'not in', ('draft', 'cancel')),
        ], limit=1)

    @api.onchange('date', 'company_id')
    def _onchange_date_update_budget(self):
        for move in self:
            if move.company_id.disable_budget_company:
                continue
            budget = move._find_budget_for_date(move.date)
            move.crossovered_budget = budget.id if budget else False
            if move.date and not budget:
                return {
                    'warning': {
                        'title': _("No Budget Found"),
                        'message': _(
                            "No Budget is configured that covers the Accounting Date %s. "
                            "Please create/confirm a Budget for that period before posting this entry."
                        ) % format_date(self.env, move.date),
                    }
                }

    def budget_id_selection_validation(self):
        # TDS-NO-BUDGET: TDS entries have no budget and no budget impact.
        for move in self.filtered(lambda l: not l.journal_id.is_opening_balance and not l.statement_line_id
                                  and not l.l10n_in_withholding_ref_move_id):
            for line1 in move.invoice_line_ids.filtered(lambda l:l.account_id.is_cash_rounding == False):
                if not move.company_id.disable_budget_company:
                    if not move.crossovered_budget:
                        if move.date and not move._find_budget_for_date(move.date):
                            raise UserError(_(
                                "No Budget is configured for the fiscal year covering the "
                                "Accounting Date %s. Please create/confirm the relevant Budget "
                                "(e.g. Budget FY covering that date) before proceeding."
                            ) % format_date(self.env, move.date))
                        raise UserError(_('Warning!! Kindly select a Budget.'))
                    if line1.budget_id and not line1.filtered(lambda e: e.analytic_distribution):
                        raise UserError(_("Alert !! Analytic Account not Mapped to %s for Entry -%s")%(
                            line1.account_id.display_name,move.display_name))


    def budget_code_selection_validation(self):
        # TDS-NO-BUDGET: TDS entries have no budget and no budget impact.
        for move in self.filtered(lambda l: not l.journal_id.is_opening_balance
                                  and not l.l10n_in_withholding_ref_move_id):
            for line1 in move.line_ids.filtered(lambda l:l.account_id.is_cash_rounding == False):
                if not move.budget_id:
                    raise UserError('Warning!! Kindly select a Budget Code.')
                if not move.budget_id.general_budget_id.account_ids:
                    raise UserError(_("Budget Code is mandatory.\n"
                                      "To proceed without a Budget Code, please enable Disable Budget Code in the respective COA."))
                # pdb.set_trace()
                if not line1.filtered(lambda e: e.analytic_distribution):
                    raise UserError(_("Alert !! Analytic Account not Mapped to %s for Entry -%s")%(
                        line1.account_id.display_name,move.display_name))
                if not line1.filtered(lambda e: {str(move.budget_id.analytic_account_id.id): 100} == e.analytic_distribution):
                    raise UserError(_("Alert !! Wrong Analytic Account Mapped to %s.\n%s is mapped to %s Budgetry Position.")%(
                        line1.account_id.display_name,move.budget_id.analytic_account_id.display_name,move.budget_id.display_name))

    @api.onchange('crossovered_budget')
    def update_budget_lines(self):
        for rec in self.line_ids:
            rec.update_budget_code()

    def update_budget_code_id(self):
        for rec in self.line_ids:
                # if rec.move_id.move_type == 'entry':
                if rec.move_id.crossovered_budget:
                    if rec.account_id:
                        budget_post = self.env['account.budget.post'].sudo().search([('account_ids.name','in',[rec.account_id.name])])
                        budget_id = rec.move_id.crossovered_budget.crossovered_budget_line.filtered(lambda l:l.general_budget_id in budget_post)
                        rec.write({'budget_id':budget_id.ids})

    def update_actual_cur_figure_server_action(self):
        record_ids = self._context.get('active_ids')
        if record_ids:
            month_list = []
            # budget_list=[]
            for rec in record_ids:
                move = self.env['account.move'].browse(rec)
                # pdb.set_trace()
                # for budget in move.budget_id.crossovered_budget_id:
                # domain1 = [('budget_id', '=', move.budget_id.crossovered_budget_id.id),('budget_position_id', '=', move.budget_id.general_budget_id.id)]
                # cur_budget_lines = self.env['crr.budget.line'].sudo().search(domain1)
                # for line in cur_budget_lines:
                #     line.write({
                #             'april_cur_budget':
                #         })
                
                month_field = month_field_map.get(move.date.month)
                if month_field not in month_list:
                # pdb.set_trace()
                    setattr(move.budget_id.crr_budget_line_id, month_field, 0)
                    print("Get attr", getattr(move.line_ids.budget_id.crr_budget_line_id, month_field))
                month_list.append(month_field)
                move.update_budget_code_id()
                # pdb.set_trace()
                # pdb.set_trace()
                if move.state in ['posted']:
                    if move.budget_update == True:
                        move.write({'budget_update': False})
                        move.action_update_budget_cur_figure_add()
                    else:
                        move.action_update_budget_cur_figure_add()
                if move.state not in ['posted']:
                    move.action_update_budget_cur_figure_minus()

    def action_update_budget_cur_figure_minus(self):
        for rec in self:

                month_field = month_field_map.get(rec.date.month)
                # Budget code is moved to line items.
                # if rec.move_type != 'entry':
                #     rec.budget_code_selection_validation()
                #     setattr(rec.budget_id.crr_budget_line_id, month_field,
                #             getattr(rec.budget_id.crr_budget_line_id, month_field) - rec.amount_untaxed)
                # else:
                # debit_value = sum(self.env['account.move.line'].sudo().search([
                #     ('move_id', '=', rec.id),  # Ensure we fetch lines from this move
                #     ('debit', '>', 0),
                #     ('account_id', '=', rec.budget_id.general_budget_id.account_ids.id),
                # ]).mapped('debit'))
                if rec.state == 'posted':
                    # balance = sum(self.env['account.move.line'].sudo().search([
                    #     ('move_id', '=', rec.id),  # Ensure we fetch lines from this move
                    #     ('account_id', '=', rec.budget_id.general_budget_id.account_ids.id),
                    # ]).mapped('balance'))
                    entry = self.env['account.move.line'].sudo().search([
                        ('move_id', '=', rec.id), ('date', '>=', rec.crossovered_budget.date_from),
                        ('date', '<=', rec.crossovered_budget.date_to),  # Ensure we fetch lines from this move
                        ('account_id', 'in', rec.crossovered_budget.crossovered_budget_line.general_budget_id.account_ids.ids),
                    ]).filtered(lambda e: {str(e.budget_id.analytic_account_id.id): 100} == e.analytic_distribution)
                    for v1 in entry:
                        balance = sum(v1.mapped('balance'))
                        for line in v1.budget_id.crr_budget_line_id:
                            setattr(line, month_field, getattr(line, month_field) - balance)
                rec.write({'budget_update': False})

    def action_update_budget_cur_figure_add(self):
        for rec in self.filtered(lambda l: not l.budget_update):

            month_field = month_field_map.get(rec.date.month)
            if month_field:
                    # Budget code is moved to line items.
                    # if rec.move_type != 'entry':
                    #     rec.budget_code_selection_validation()
                    #     setattr(rec.budget_id.crr_budget_line_id, month_field,
                    #             getattr(rec.budget_id.crr_budget_line_id, month_field) + rec.amount_untaxed)
                    # else:
                    rec.budget_id_selection_validation()
                    domain12 = [('move_id', '=', rec.id), ('date', '>=', rec.crossovered_budget.date_from),('date', '<=', rec.crossovered_budget.date_to),('account_id', 'in', rec.crossovered_budget.crossovered_budget_line.general_budget_id.account_ids.ids)]
                    entry = self.env['account.move.line'].sudo().search(domain12).filtered(lambda e: {str(e.budget_id.analytic_account_id.id): 100} == e.analytic_distribution)
                    for v1 in entry:
                        balance = sum(v1.mapped('balance'))
                        for line in v1.budget_id.crr_budget_line_id:
                            setattr(line, month_field, getattr(line, month_field) + balance)
            rec.write({'budget_update': True})

    def action_update_account_move_tax_grids(self):
        ###Update Tax Grids
        records = self.env['account.move'].browse(self._context.get('active_ids', False))
        for record in records:
            for line in record.line_ids.filtered(lambda l: l.tax_line_id):
                if not line.tax_tag_ids:
                    my_list =[]
                    if record.move_type in ['out_invoice', 'in_invoice', 'out_receipt', 'in_receipt', 'entry']:
                        for tax_grid in line.tax_line_id.invoice_repartition_line_ids.filtered(lambda t: t.tag_ids):
                            tax_grid_values = tax_grid.tag_ids.ids
                            line.write({'tax_tag_ids': tax_grid_values})
                    if record.move_type in ['out_refund', 'in_refund']:
                        for tax_grid in line.tax_line_id.refund_repartition_line_ids.filtered(lambda t: t.tag_ids):
                            tax_grid_values = tax_grid.tag_ids.ids
                            line.write({'tax_tag_ids': tax_grid_values})

    def action_validate_no_bill(self):
        for move in self.filtered(lambda l: l.move_type in ['in_invoice']):
            if not move.invoice_date:
                raise UserError(_("Alert !! Please update the Vendor Bill Date."))
            po_threshold_amount_formatted = formatLang(self.env, move.company_id.po_threshold_amount, currency_obj=move.company_id.currency_id)
            if move.company_id.po_threshold_amount <=0:
                raise UserError(_("Alert !! Please define the PO Threshold Amount to post the Vendor Bill."))
            if move.company_id.po_threshold_amount< move.amount_total:
                if not move.line_ids.purchase_line_id.order_id:
                    raise UserError(_("Alert !! You cannot post a Vendor Bill without linking it to Purchase Order as it exceeds the PO Threshold Amount of %s")
                        %(po_threshold_amount_formatted))
                # raise UserError(_("Alert !! You cannot post a Vendor Bill above the PO Threshold Amount."))

    def action_post(self):
        for rec in self:
            if rec.advance_payment_ids:
                rec.l10n_in_withhold_move_ids = [(6, 0, rec.l10n_in_withhold_move_ids.ids + rec.advance_payment_ids.move_id.ids)]
            purchase_order = self.line_ids.purchase_line_id.order_id
            if purchase_order:
                purchase_order.budget_id.reserved_amount -= rec.amount_untaxed
            # for line in rec.line_ids.filtered(lambda l: l.account_id.account_type in ['asset_fixed', 'expense']):
            #     if not rec.budget_id:
            #         raise UserError('Warning!! Kindly select a Budget Code.')
            # rec.budget_code_selection_validation()
            rec.action_update_budget_cur_figure_add()
            rec.action_validate_no_bill()
        res = super(AccountMoveInherit, self).action_post()
        for rec in self:
            if rec.move_type != 'entry':
                continue

                # Find Salary Payable line (10321002)
            salary_lines = rec.line_ids.filtered(
                lambda l: l.account_id.code == '10321002' and l.credit > 0
            )

            if not salary_lines:
                continue

            # If multiple lines → sum
            amount = sum(salary_lines.mapped('credit'))

            existing = self.env['account.move'].search([
                ('ref', 'ilike', rec.ref),
                ('journal_id.code', '=', 'ICI97')
            ], limit=1)

            if existing:
                continue

            # Accounts
            debit_account = self.env['account.account'].search([
                ('code', '=', '10321002')
            ], limit=1)

            credit_account = self.env['account.account'].search([
                ('code', '=', '100204')
            ], limit=1)

            if not debit_account or not credit_account:
                continue

            # Journal
            journal = self.env['account.journal'].search([
                ('code', '=', 'ICI97')
            ], limit=1)

            if not journal:
                continue

            ref_text = rec.ref or ''

            month_full = ''
            year = ''

            match = re.search(r'(\w+)\s+(\d{4})', ref_text)
            if match:
                month_short = match.group(1)
                year = match.group(2)

                try:
                    month_full = calendar.month_name[
                        list(calendar.month_abbr).index(month_short[:3])
                    ]
                except:
                    month_full = month_short

            new_ref = f"Salary payment for the month of {month_full} {year}" if month_full else rec.name
            new_move = self.env['account.move'].create({
                'move_type': 'entry',
                'journal_id': journal.id,
                'date': fields.Date.today(),
                'ref': new_ref,
                'line_ids': [
                    (0, 0, {
                        'account_id': debit_account.id,
                        'debit': amount,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'account_id': credit_account.id,
                        'debit': 0.0,
                        'credit': amount,
                    }),
                ]
            })

            new_move.action_post()
            if rec.advance_payment_ids:
                self.activity_schedule(
                    activity_type_id=rec.env.ref('mail.mail_activity_data_todo').id,
                    summary=f"Kindly Check if you have add TDS for the bill {rec.name}",
                    note=f"Kindly Check if you have add TDS.",
                    user_id=rec.create_uid.id,
                    date_deadline=fields.Date.today()
                )
        for rec in self:
            if rec.move_type != 'entry' and rec.invoice_date and rec.invoice_date < fields.Date.today():
                if rec.move_type == 'out_invoice':
                    move_type = "Invoice"
                elif rec.move_type == 'in_invoice':
                    move_type = "Bill"
                elif rec.move_type == 'out_refund':
                    move_type = "Customer Credit Note"
                elif rec.move_type == 'in_refund':
                    move_type = "Vendor Credit Note"
                elif rec.move_type == 'out_receipt':
                    move_type = "Sales Receipt"
                else:
                    move_type = "Purchase Receipt"
                # raise UserError('You cannot post the %s with a back date' % move_type)
            # if rec.move_type == 'in_invoice' and rec.partner_id.tds_applicable:
            if rec.move_type == 'in_invoice' and rec.partner_id.tds_applicable and 'TDS' not in rec.invoice_line_ids.tax_ids.tax_group_id.mapped(
                    'name'):
                if not rec.partner_id.tds_tax_id:
                    raise UserError('Please add TDS Tax for the Vendor.')
                if not rec.amount_untaxed:
                    raise UserError('The Untaxed Amount in the bill is Zero. Please add price for Products.')
                wiz_tds = self.env['l10n_in.withhold.wizard'].with_context({
                    'active_ids': rec.ids,  # Pass the active record ID
                    'active_model': self._name  # Pass the current model name
                }).create({})
                wiz_line_tds = self.env['l10n_in.withhold.wizard.line'].create({
                    'withhold_id': wiz_tds.id,
                    'tax_id': rec.partner_id.tds_tax_id.id,
                    'base': rec.amount_untaxed,
                })
                wiz_tds.action_create_and_post_withhold()
        return res

    def action_print_invoice_template(self):
        return self.env.ref('accounts_extended.print_invoice_template1').report_action(self)

    def _compute_l10n_in_total_withholding_amount(self):
        for move in self:
            move.l10n_in_total_withholding_amount = sum(move.l10n_in_withhold_move_ids.filtered(
                lambda m: m.state == 'posted').l10n_in_withholding_line_ids.mapped('l10n_in_withhold_tax_amount'))
            if self.advance_payment_ids:
                advance_amount = 0
                for line_ids in self.advance_payment_ids.move_id.line_ids:
                    if line_ids.tax_tag_ids:
                        advance_amount +=abs(line_ids.amount_currency)
                move.l10n_in_total_withholding_amount+=round(advance_amount)

    def action_print_jv_cheque(self):
        return self.env.ref('odoo_print_cheque.print_cheque_payment_account_move').report_action(self)

    def action_export_salary_jv_xlsx(self):

        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output)
        sheet = workbook.add_worksheet('Salary JV')

        # Formats
        bold = workbook.add_format({'bold': True,})
        bold1 = workbook.add_format({'bold': True,'fg_color':'#D3D3D3'})
        input_style = workbook.add_format({'font_color': 'red'})
        date_format = workbook.add_format({'num_format': 'yyyy-mm-dd'})
        amount_format1 = workbook.add_format({'bold': True, 'fg_color': '#D3D3D3','num_format': '#,##0.00'})
        total_style = workbook.add_format({'num_format': '#,##0.00','align': 'right'})
        # Define Headers
        payslip_ref = html2plaintext(self.narration)
        payslip = self.env['hr.payslip'].sudo().search([('number', '=', str(payslip_ref))])
        month = payslip.date_from.strftime('%B')  # Full month name: "May"
        year = payslip.date_from.strftime('%Y')
        total_salary_per_month = 0
        basic_da_per_month = 0
        table_headers = ['Account Head', 'DR', 'CR']
        sheet.write(0, 0, self.company_id.name,bold)
        sheet.write(2, 0, 'Employee Payroll', bold)
        sheet.write(4, 0, 'Financial Year', bold)
        sheet.write(4, 3, 'Month Year', bold)
        sheet.write(4, 1, year,input_style)
        sheet.write(4, 4, month + ' ' + year,input_style)
        for col, header in enumerate(table_headers):
            sheet.write(6, col, header, bold1)
        amount_format = workbook.add_format({'num_format': '#,##0.00','align': 'right'})
        #
        # # Populate Data
        row = 7
        for index, line in enumerate(self.line_ids, start=1):
            sheet.write(row, 0, line.account_id.name or '')
            sheet.write(row, 1, line.debit or '0.0', amount_format)
            sheet.write(row, 2, line.credit or '0.0', amount_format)
            row += 1
        row = row+1
        sheet.write(row, 0, 'Total', bold1)
        sheet.write(row, 1, sum(self.line_ids.mapped('debit')), amount_format1)
        sheet.write(row, 2, sum(self.line_ids.mapped('credit')), amount_format1)
        row = row+1
        basic_salary  =payslip.line_ids.filtered(lambda l: l.name == 'Basic Salary')
        food_coupons = payslip.line_ids.filtered(lambda l: l.name == 'Food Coupons')
        row = row + 2
        sheet.write(row, 0, 'Employee Name',bold)
        sheet.write(row, 1, 'Employee ID',bold)
        sheet.write(row, 2, 'Salary On Hold',bold)
        sheet.write(row, 3, 'Parental Insurance',bold)
        sheet.write(row, 4, 'Food Coupon', bold)
        row = row + 1
        sheet.write(row, 0, payslip.employee_id.name)
        sheet.write(row, 1, payslip.employee_id.employee_number)
        sheet.write(row, 2, basic_salary.total or 0.0,total_style)
        sheet.write(row, 3, food_coupons.total or 0.0,total_style)
        sheet.write(row, 4, food_coupons.total or 0.0,total_style)
        row=row+2
        # sheet.write(row, 1, 'As per input',bold)
        # sheet.write(row, 2, basic_salary.total or 0.0,total_style)
        # sheet.write(row, 3, food_coupons.total or 0.0,total_style)
        # sheet.write(row, 4, food_coupons.total or 0.0,total_style)
        # row=row+1
        # sheet.write(row, 1, 'Diff',bold)
        # sheet.write(row, 2, '-')
        # sheet.write(row, 3, '-')
        # sheet.write(row, 4, '-')
        # sheet.write(row, 4, 'Employee ID', bold)
        sheet.set_column(0, 0, 25)
        sheet.set_column(1, 1, 15)
        sheet.set_column(2, 2, 15)
        sheet.set_column(3, 3, 20)
        sheet.set_column(4, 4, 20)
        sheet.set_column(5, 5, 25)
        sheet.set_column(6, 6, 15)
        sheet.set_column(7, 7, 30)
        sheet.set_column(8, 8, 25)
        sheet.set_column(9, 9, 20)
        sheet.set_column(10, 10, 20)
        sheet.set_column(11, 11, 25)
        sheet.set_column(12, 12, 30)
        sheet.set_column(13, 13, 40)

        workbook.close()
        output.seek(0)

        # Encode File to Base64
        file_data = base64.b64encode(output.read())
        output.close()

        # Create Attachment
        attachment = self.env['ir.attachment'].create({
            'name': f'Salary_JV_{datetime.now().strftime("%Y%m%d%H%M%S")}.xlsx',
            'type': 'binary',
            'datas': file_data,
            'store_fname': f'Salary_JV_{datetime.now().strftime("%Y%m%d%H%M%S")}.xlsx',
            'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'res_model': 'account.move',
            'res_id': self.id,
        })

        # Return the attachment download URL
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }


class AccountAnalyticPlan(models.Model):
    _inherit = 'account.analytic.plan'

    active = fields.Boolean(default=True)


class AccountTax(models.Model):
    _inherit = 'account.tax'

    def _prepare_tax_totals(self, base_lines, currency, tax_lines=None, is_company_currency_requested=False):
        result = super()._prepare_tax_totals(base_lines, currency, tax_lines=tax_lines, is_company_currency_requested=is_company_currency_requested)
        for subtotal in result['subtotals']:
            if subtotal['name'] == _("Untaxed Amount"):
                subtotal['name'] = _("Taxable Amount")

        result['subtotals_order'] = [
            _("Taxable Amount") if name == _("Untaxed Amount") else name
            for name in result['subtotals_order']
        ]
        if _("Untaxed Amount") in result['groups_by_subtotal']:
            result['groups_by_subtotal'][_("Taxable Amount")] = result['groups_by_subtotal'].pop(_("Untaxed Amount"))
        return result

