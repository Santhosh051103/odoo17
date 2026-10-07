from odoo import api, fields, models, _, Command, tools
from odoo.addons.base.models.decimal_precision import DecimalPrecision
from odoo.exceptions import UserError, ValidationError, AccessError, RedirectWarning
from bisect import bisect_left
from collections import defaultdict
from math import *
import datetime
import re
import random
import secrets
import pdb
from datetime import date, timedelta, datetime
from num2words import num2words
from odoo.osv import expression
from odoo.tools import format_amount, format_date, formatLang, groupby
from odoo.tools.float_utils import float_is_zero
from markupsafe import Markup
import ast


class PurchaseOrderInherit(models.Model):
    _inherit = "purchase.order"

    purchase_type = fields.Many2one('purchase.orders.type', 'Purchase Type', required=1)
    budget_id = fields.Many2one('crossovered.budget.lines', 'Budget Code', copy=False, )
    budget_balance_warning = fields.Html(
        compute='_compute_budget_balance_warning',
    )

    approval_state = fields.Char(string='Approval Status', compute='compute_approval_state', store=True, copy=False,
                                 tracking=True)
    approval_document = fields.Many2one('multi.approval', string='Approval Record', copy=False)
    approval_history = fields.Text(string="Approval History", readonly=True,
                                   help="Tracks approval reasons and metadata", copy=False)
    quote_matrix_approval_state = fields.Selection([
        ('draft', 'Draft'),
        ('quote_exceeds', 'Quote Exceeds'),
        ('to_department_head', 'To Department Head'),
        ('to_account_head', 'To Account Head'),
        ('to_tax_entity_head', 'To Tax Entity Head'),
        ('approved', 'Approved'),
    ], string="Status", readonly=True, index=True, default='draft', tracking=True, copy=False)
    rfq_reminder_sent = fields.Boolean(string="RFQ Reminder Sent", default=False, copy=False)
    rfq_lock_applied = fields.Boolean(default=False, copy=False)

    def portal_url(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        return f"{base_url}/web/login"

    @api.model
    def cron_send_rfq_vendor_reminder(self):
        now = fields.Datetime.now()
        deadline_reminder = now - timedelta(days=5)
        deadline_lock = now - timedelta(days=12)

        rfqs = self.search([
            ('state', '=', 'sent'),
            ('partner_id', '!=', False),
        ])
        processed_partners = set()

        for rfq in rfqs:
            partner = rfq.partner_id
            last_vendor_reply = max(
                (
                    msg.date for msg in rfq.message_ids
                    if msg.author_id == rfq.partner_id and msg.message_type == 'comment'
                ),
                default=None
            )

            no_recent_reply = (
                    not last_vendor_reply or
                    last_vendor_reply <= deadline_reminder
            )

            # 5th-day reminder
            if no_recent_reply and rfq.date_order <= deadline_reminder and not rfq.rfq_reminder_sent:
                template = self.env.ref('purchase_order_extended.rfq_vendor_reminder_template',
                                        raise_if_not_found=False)
                if template:
                    template.send_mail(rfq.id, force_send=True)
                    rfq.rfq_reminder_sent = True

            # 12th-day password lock
            if no_recent_reply and rfq.date_order <= deadline_lock:
                if partner.id not in processed_partners and not rfq.rfq_lock_applied:
                    locked_users = []
                    for user in rfq.partner_id.user_ids:
                        new_password = secrets.token_urlsafe(24)
                        user.sudo().write({'password': new_password})
                        locked_users.append(user.name)

                        template_notify = self.env.ref(
                            'purchase_order_extended.vendor_password_change_notify_template',
                            raise_if_not_found=False
                        )
                        if template_notify:
                            template_notify.sudo().with_context(
                                user_name=user.name,
                                vendor_name=partner.name,
                                rfq_name=rfq.name,
                                
                            ).send_mail(rfq.id, force_send=True)

                        if user.partner_id:
                            user.partner_id.message_post(
                                body=(
                                    f"🔒 Your password has been changed by the system due to no response on RFQ {rfq.name} "
                                    f"for over 12 days. Please contact the purchase team."
                                ),
                                message_type="comment",
                                subtype_xmlid="mail.mt_note"
                            )

                    if locked_users:
                        rfq.message_post(
                            body=f"⚠️ No response received from vendor after 12 days. "
                                 f"Password changed for user(s): {', '.join(locked_users)}."
                        )
                    rfq.rfq_lock_applied = True
                    processed_partners.add(partner.id)
                elif partner.id in processed_partners and not rfq.rfq_lock_applied:
                    rfq.message_post(
                        body="⚠️ No response received from vendor after 12 days. "
                             f"Password already changed for this user."
                    )
                    rfq.rfq_lock_applied = True

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
                    [('model_id', '=', 'purchase.order'), ('state', '=', 'confirm')], limit=1)
                if rec:
                    record.approval_state = 'To Submit for Approval'
                else:
                    record.approval_state = 'Not Applicable'

    @api.depends('budget_id', 'company_id', 'partner_id', 'amount_total', 'currency_id', 'order_line.product_qty',
                 'order_line.price_unit', 'amount_untaxed')
    def _compute_budget_balance_warning(self):
        msg = ''
        for order in self.filtered(lambda s: s.budget_id):
            order.with_company(order.company_id)
            order.budget_balance_warning = ''
            po_date = order.date_order or fields.Date.today()
            # domain1 = [('date_from', '<=', po_date), ('date_to', '>=', po_date),('id', '=', order.budget_id.id)]
            domain1 = [('id', '=', order.budget_id.id)]
            budget_allocated_id = self.env['crossovered.budget.lines'].sudo().search(domain1, limit=1)
            if budget_allocated_id:
                allocated_amount = budget_allocated_id.planned_amount
                spent_amount = (abs(budget_allocated_id.practical_amount) + budget_allocated_id.reserved_amount)
                available_amount = allocated_amount - spent_amount
                allocated_amount_formatted = formatLang(self.env, allocated_amount,
                                                        currency_obj=order.company_id.currency_id)
                available_amount_formatted = formatLang(self.env, available_amount,
                                                        currency_obj=order.company_id.currency_id)
                if order.amount_total > available_amount:
                    budget_url = "/web#id=%s&model=crossovered.budget&view_type=form" % order.budget_id.crossovered_budget_id.id
                    msg = Markup(
                        "<span style='color: red;'>Alert !! Budget is exceeding for "
                        "<a href='%s' target='_blank' style='color: blue; text-decoration: underline;'>%s</a>."
                        " Allocated budget is %s and Available balance is %s.</span>"
                    ) % (budget_url, order.budget_id.display_name, allocated_amount_formatted,
                         available_amount_formatted)
                else:
                    msg = Markup(
                        "For %s Allocated budget is %s and Available balance is %s."
                    ) % (order.budget_id.display_name, allocated_amount_formatted, available_amount_formatted)
            else:
                msg = Markup("Alert !! No active budget found.</span>")
        self.budget_balance_warning = msg
        total_amount = self.amount_total

        other_pos = self.sudo().search([
            ('state', 'not in', ['done', 'cancel', 'purchase'])
        ])
        total_other_po = []
        for other_po in other_pos:
            if sorted(other_po.order_line.mapped('product_id').ids) == sorted(self.order_line.mapped('product_id').ids):
                # total_amount += (other_po.amount_total)
                total_other_po.append(other_po)

        # Fetch configuration settings
        level_1 = float(self.company_id.po_value_1)
        quotes_1 = int(self.company_id.quotes_required_1)
        level_2 = float(self.company_id.po_value_2)
        quotes_2 = int(self.company_id.quotes_required_2)
        level_3 = float(self.company_id.po_value_3)
        quotes_3 = int(self.company_id.quotes_required_3)
        # Compare and validate levels
        warning = False
        if total_amount <= level_1 and len(
                total_other_po) < quotes_1 and self.quote_matrix_approval_state != 'approved':
            warning = True
        elif total_amount > level_1 and total_amount <= level_2 and len(
                total_other_po) < quotes_2 and self.quote_matrix_approval_state != 'approved':
            warning = True
        elif total_amount > level_2 and len(
                total_other_po) < quotes_3 and self.quote_matrix_approval_state != 'approved':
            warning = True
        if warning == True and self.quote_matrix_approval_state == 'draft':
            self.quote_matrix_approval_state = 'quote_exceeds'
        elif warning == False and self.quote_matrix_approval_state not in ('draft', 'approved'):
            self.quote_matrix_approval_state = 'draft'

    def _prepare_invoice(self):
        invoice_vals = super()._prepare_invoice()
        # self.ensure_one()
        if self.budget_id:
            invoice_vals['budget_id'] = self.budget_id.id
        return invoice_vals

    def exceed_budget_balance_warning(self):
        msg = ''
        for order in self.filtered(lambda s: s.budget_id):
            order.with_company(order.company_id)
            order.budget_balance_warning = ''
            po_date = order.date_order or fields.Date.today()
            # domain1 = [('date_from', '<=', po_date), ('date_to', '>=', po_date), ('id', '=', order.budget_id.id)]
            domain1 = [('id', '=', order.budget_id.id)]
            budget_allocated_id = self.env['crossovered.budget.lines'].sudo().search(domain1, limit=1)
            if budget_allocated_id:
                allocated_amount = budget_allocated_id.planned_amount
                spent_amount = (abs(budget_allocated_id.practical_amount) + budget_allocated_id.reserved_amount)
                available_amount = allocated_amount - spent_amount
                allocated_amount_formatted = formatLang(self.env, allocated_amount,
                                                        currency_obj=order.company_id.currency_id)
                available_amount_formatted = formatLang(self.env, available_amount,
                                                        currency_obj=order.company_id.currency_id)
                if order.amount_total > available_amount:
                    msg = "Alert !! Budget is exceeding for %s. Allocated budget is %s and Available balance is %s." % (
                        order.budget_id.display_name, allocated_amount_formatted, available_amount_formatted)
                    raise UserError(_(msg))

    def button_send_for_approval(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Approval Reason',
            'res_model': 'purchase.order.approval.wizard',
            'view_mode': 'form',
            'target': 'new',
        }

    def button_confirm(self):
        for order in self.filtered(lambda c: c.state in ['draft', 'sent', 'to approve']):
            if not order.budget_id:
                raise UserError(_("Alert !! Please select the budget."))
            if order.budget_id.crossovered_budget_id.state not in ['done']:
                raise UserError(_("Alert !! The Budget is not approved."))
            if not order.order_line:
                raise UserError(_("Alert !! Please select Products."))
            if not order.purchase_type:
                raise UserError(_("Alert !! Please select the Purchase Type for %s to confirm.") % (order.display_name))
            total_amount = order.amount_total

            other_pos = self.search([
                ('state', 'not in', ['done', 'cancel', 'purchase']),
                ('id', '!=', order.id)
            ])
            total_other_po = []
            for other_po in other_pos:
                if sorted(other_po.order_line.mapped('product_id').ids) == sorted(
                        order.order_line.mapped('product_id').ids):
                    # total_amount += (other_po.amount_total)
                    total_other_po.append(other_po)

            # Fetch configuration settings
            level_1 = float(order.company_id.po_value_1)
            quotes_1 = int(order.company_id.quotes_required_1)
            level_2 = float(order.company_id.po_value_2)
            quotes_2 = int(order.company_id.quotes_required_2)
            level_3 = float(order.company_id.po_value_3)
            quotes_3 = int(order.company_id.quotes_required_3)
            # Compare and validate levels
            if total_amount <= level_1 and len(
                    total_other_po) + 1 < quotes_1 and self.quote_matrix_approval_state != 'to_tax_entity_head':
                raise UserError(_("PO Value exceeds Level 1. Minimum %s quotes required.") % quotes_1)
            elif total_amount > level_1 and total_amount <= level_2 and len(
                    total_other_po) + 1 < quotes_2 and self.quote_matrix_approval_state != 'to_tax_entity_head':
                raise UserError(_("PO Value exceeds Level 2. Minimum %s quotes required.") % quotes_2)
            elif total_amount > level_2 and len(
                    total_other_po) + 1 < quotes_3 and self.quote_matrix_approval_state != 'to_tax_entity_head':
                raise UserError(_("PO Value exceeds Level 3. Minimum %s quotes required.") % quotes_3)
            if self.quote_matrix_approval_state == 'to_tax_entity_head' and not self.env.user.has_group(
                    'accounts_extended.group_tax_entity_director'):
                raise UserError(_("You are not authorized to approve this PO."))
            elif self.quote_matrix_approval_state == 'to_tax_entity_head' and self.env.user.has_group(
                    'accounts_extended.group_tax_entity_director'):
                self.quote_matrix_approval_state = 'approved'
            for line in order.order_line:
                # Check if the price is zero or less
                if line.price_unit <= 0:
                    raise ValidationError(_(
                        "The price of the product '%s'"
                        "cannot be zero or less. Please correct it before confirming."
                    ) % (line.product_id.display_name))
            order.exceed_budget_balance_warning()
            order.write({'state': 'sent'})
            # Update a reserve amount in budget
            order.budget_id.reserved_amount += order.amount_untaxed
        return super(PurchaseOrderInherit, self).button_confirm()

    @api.onchange('requisition_id')
    def _onchange_requisition_id(self):
        if not self.requisition_id:
            return

        self = self.with_company(self.company_id)
        requisition = self.requisition_id
        if self.partner_id:
            partner = self.partner_id
        else:
            partner = requisition.vendor_id
        payment_term = partner.property_supplier_payment_term_id

        FiscalPosition = self.env['account.fiscal.position']
        fpos = FiscalPosition.with_company(self.company_id)._get_fiscal_position(partner)

        self.partner_id = partner.id
        self.fiscal_position_id = fpos.id
        self.payment_term_id = payment_term.id
        self.company_id = requisition.company_id.id
        self.currency_id = requisition.currency_id.id
        if not self.origin or requisition.name not in self.origin.split(', '):
            if self.origin:
                if requisition.name:
                    self.origin = self.origin + ', ' + requisition.name
            else:
                self.origin = requisition.name
        self.notes = requisition.description
        self.date_order = fields.Datetime.now()

        if requisition.type_id.line_copy != 'copy':
            return

        # Create PO lines if necessary
        order_lines = []
        for line in requisition.line_ids:
            # Compute name
            product_lang = line.product_id.with_context(
                lang=partner.lang or self.env.user.lang,
                partner_id=partner.id
            )
            name = product_lang.display_name
            if product_lang.description_purchase:
                name += '\n' + product_lang.description_purchase

            # Compute taxes
            taxes_ids = fpos.map_tax(
                line.product_id.supplier_taxes_id.filtered(lambda tax: tax.company_id == requisition.company_id)).ids

            # Compute quantity and price_unit
            if line.product_uom_id != line.product_id.uom_po_id:
                product_qty = line.product_uom_id._compute_quantity(line.product_qty, line.product_id.uom_po_id)
                price_unit = line.product_uom_id._compute_price(line.price_unit, line.product_id.uom_po_id)
            else:
                product_qty = line.product_qty
                price_unit = line.price_unit

            if requisition.type_id.quantity_copy != 'copy':
                product_qty = 0

            # Create PO line
            # order_line_values = line._prepare_purchase_order_line(
            #     name=name, product_qty=product_qty, price_unit=price_unit,
            #     taxes_ids=taxes_ids)
            # order_lines.append((0, 0, order_line_values))
        self.order_line = order_lines


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    def _prepare_account_move_line(self, move=False):
        self.ensure_one()
        aml_currency = move and move.currency_id or self.currency_id
        date = move and move.date or fields.Date.today()
        res = {
            'display_type': self.display_type or 'product',
            'name': '%s: %s' % (self.order_id.name, self.name),
            'product_id': self.product_id.id,
            'product_uom_id': self.product_uom.id,
            'quantity': self.qty_to_invoice,
            'discount': self.discount,
            'price_unit': self.currency_id._convert(self.price_unit, aml_currency, self.company_id, date, round=False),
            'tax_ids': [(6, 0, self.taxes_id.ids)],
            'purchase_line_id': self.id,
        }
        if self.analytic_distribution and not self.display_type:
            res['analytic_distribution'] = self.analytic_distribution
        if self.order_id.budget_id.analytic_account_id:
            distribution = {}

            distribution[self.order_id.budget_id.analytic_account_id.id] = 100.0

            res['analytic_distribution'] = distribution
        return res


class MailComposeMessage(models.TransientModel):
    _inherit = 'mail.compose.message'

    def _action_send_mail(self, auto_commit=False):
        result_mails_su, result_messages = super()._action_send_mail(auto_commit=auto_commit)

        for wizard in self:
            if wizard.model == 'purchase.order' and wizard.res_ids:
                try:
                    res_ids_list = ast.literal_eval(wizard.res_ids)
                    for res_id in res_ids_list:
                        order = self.env['purchase.order'].browse(res_id)
                        order.rfq_reminder_sent = False
                        order.rfq_lock_applied = False
                except (SyntaxError, ValueError):
                    print("Failed to parse res_ids for Purchase Order in mail wizard")

        return result_mails_su, result_messages
