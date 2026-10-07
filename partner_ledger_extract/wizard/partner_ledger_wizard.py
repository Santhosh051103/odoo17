# -*- coding: utf-8 -*-
from odoo import fields, models

VCH_TYPE_MAP = {
    'entry': 'Journal',
    'out_invoice': 'Sales',
    'out_refund': 'Credit Note',
    'in_invoice': 'Purchase',
    'in_refund': 'Debit Note',
    'out_receipt': 'Receipt',
    'in_receipt': 'Payment',
}


class PartnerLedgerWizard(models.TransientModel):
    _name = 'partner.ledger.wizard'
    _description = 'Partner Ledger Extract Wizard'

    partner_ids = fields.Many2many(
        'res.partner', string='Partners', required=True,
        help='One PDF section is printed per selected partner.')
    date_from = fields.Date(
        string='Date From',
        help='Leave empty to include everything up to Date To with no opening-balance line.')
    date_to = fields.Date(
        string='Date To', required=True, default=fields.Date.context_today)
    target_move = fields.Selection([
        ('posted', 'All Posted Entries'),
        ('all', 'All Entries (incl. drafts)'),
    ], string='Target Moves', default='posted', required=True)

    def action_print_report(self):
        self.ensure_one()
        return self.env.ref(
            'partner_ledger_extract.action_report_partner_ledger'
        ).report_action(self)

    # ------------------------------------------------------------------
    # Small formatting helpers (kept in Python so the QWeb template
    # doesn't need platform-specific strftime flags like %-d).
    # ------------------------------------------------------------------
    def fmt_date(self, date):
        if not date:
            return ''
        return '%d-%s-%s' % (date.day, date.strftime('%b'), date.strftime('%y'))

    def get_partner_address(self, partner):
        parts = [p for p in (partner.street, partner.street2, partner.city) if p]
        return ', '.join(parts)

    # ------------------------------------------------------------------
    # Report data helpers
    # ------------------------------------------------------------------
    def _get_move_line_domain(self, partner):
        """Every receivable/payable line for this partner up to date_to.
        account_type covers customer invoices, credit notes, vendor bills,
        payments and receipts alike, so both sides of the ledger show up."""
        domain = [
            ('partner_id', '=', partner.id),
            ('account_id.account_type', 'in', ('asset_receivable', 'liability_payable')),
            ('display_type', 'not in', ('line_section', 'line_note')),
        ]
        if self.target_move == 'posted':
            domain.append(('parent_state', '=', 'posted'))
        if self.date_to:
            domain.append(('date', '<=', self.date_to))
        return domain

    def _get_vch_type(self, move):
        if move.payment_id:
            return 'Receipt' if move.payment_id.payment_type == 'inbound' else 'Payment'
        return VCH_TYPE_MAP.get(move.move_type) or 'Journal'

    def _get_counter_lines(self, line):
        """Every OTHER line on the same journal entry (i.e. every account
        this voucher actually touched besides the partner's own
        receivable/payable line) -- this is what lets a TDS-split entry
        show its full break-up the way Tally's 'as per details' does."""
        others = line.move_id.line_ids.filtered(
            lambda l: l.id != line.id and l.display_type not in ('line_section', 'line_note')
        )
        counter_lines = []
        for cl in others:
            counter_lines.append({
                'code': cl.account_id.code or '',
                'name': cl.account_id.name or '',
                'amount': cl.debit or cl.credit,
                'side': 'Dr' if cl.debit else 'Cr',
            })
        return counter_lines

    def get_partner_data(self, partner):
        Line = self.env['account.move.line']
        full_domain = self._get_move_line_domain(partner)

        opening_balance = 0.0
        period_domain = full_domain
        if self.date_from:
            opening_domain = full_domain + [('date', '<', self.date_from)]
            opening_lines = Line.search(opening_domain)
            opening_balance = sum(opening_lines.mapped('debit')) - sum(opening_lines.mapped('credit'))
            period_domain = full_domain + [('date', '>=', self.date_from)]

        move_lines = Line.search(period_domain, order='date asc, id asc')

        result_lines = []
        period_debit = 0.0
        period_credit = 0.0
        for line in move_lines:
            period_debit += line.debit
            period_credit += line.credit
            result_lines.append({
                'date_display': self.fmt_date(line.date),
                'vch_type': self._get_vch_type(line.move_id),
                'vch_no': line.move_id.name or '/',
                'debit': line.debit,
                'credit': line.credit,
                'counter_lines': self._get_counter_lines(line),
                'narration': line.name or line.move_id.ref or '',
            })

        opening_debit = max(opening_balance, 0.0)
        opening_credit = max(-opening_balance, 0.0)
        total_debit = opening_debit + period_debit
        total_credit = opening_credit + period_credit

        # Classic ledger balancing: whichever side is smaller gets a
        # "Closing Balance" plug so both column totals end up equal,
        # labelled with the OPPOSITE side to how Tally displays it.
        diff = total_debit - total_credit
        closing_side = None
        closing_amount = 0.0
        grand_total = max(total_debit, total_credit)
        if diff > 0.005:
            closing_side = 'Cr'
            closing_amount = diff
            grand_total = total_debit
        elif diff < -0.005:
            closing_side = 'Dr'
            closing_amount = -diff
            grand_total = total_credit

        return {
            'opening_balance': opening_balance,
            'opening_debit': opening_debit,
            'opening_credit': opening_credit,
            'lines': result_lines,
            'total_debit': total_debit,
            'total_credit': total_credit,
            'closing_side': closing_side,
            'closing_amount': closing_amount,
            'grand_total': grand_total,
        }
