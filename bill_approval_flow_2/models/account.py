from odoo import models, api, _,fields
from odoo.exceptions import UserError
from odoo.tools import float_compare
import logging
_logger = logging.getLogger(__name__)  # SPLIT-DBG


class AccountMove(models.Model):
    _inherit = 'account.move'

    approval_id = fields.Many2one('bill.approval', string="Approvals")

    def _copy_approval_attachments(self, approval_id):
        if not approval_id:
            return

        attachments = self.env['ir.attachment'].search([
            ('res_model', '=', 'bill.approval'),
            ('res_id', '=', approval_id)
        ])

        for att in attachments:
            existing = self.env['ir.attachment'].search([
                ('res_model', '=', 'account.move'),
                ('res_id', '=', self.id),
                ('name', '=', att.name),
            ], limit=1)

            if not existing:
                att.copy({
                    'res_model': 'account.move',
                    'res_id': self.id,
                })

    # ---------------- BILL LIMIT CHECK ----------------
    # CONTROL: bill booking under an approval can never go beyond the
    # approved Taxable amount or the approved GST amount, and each is
    # checked against its own bucket only (taxable vs taxable, GST vs GST):
    #
    #   * Taxable consumed by a bill = its amount_untaxed. TDS never
    #     reduces this. TDS is part of the taxable amount (paid to the
    #     Government on the vendor's behalf), so however TDS is applied
    #     (negative tax line on the invoice lines, TDS smart button on the
    #     bill, TDS smart button on the payment) the full taxable base is
    #     still consumed from the approval.
    #   * GST consumed by a bill = its GST tax lines only (matched by tax
    #     name containing "gst"), so TDS lines can never leak into it.
    #
    # There is deliberately NO total-vs-total comparison any more: it mixed
    # the two buckets (and, being net of tax-line TDS, let taxable slip
    # through). Cancelled bills are ignored so a cancelled bill releases
    # its share of the approval.
    def _get_taxable_gst_amounts(self):
        """(taxable, gst) consumed by the moves in self."""
        taxable = sum(self.mapped('amount_untaxed'))
        gst_lines = self.line_ids.filtered(
            lambda l: l.tax_line_id and 'gst' in (l.tax_line_id.name or '').lower()
        )
        gst = abs(sum(gst_lines.mapped('balance')))
        return taxable, gst

    @api.model
    def _billed_position(self, approval, exclude_ids=None):
        """(billed_taxable, billed_gst) actually BOOKED on this approval's
        bills right now -- not the approved figures. Cancelled bills are
        excluded, so is a bill passed in `exclude_ids` (used when checking
        whether cancelling THAT bill would still be safe)."""
        domain = [('approval_id', '=', approval.id), ('move_type', '!=', 'entry'), ('state', '!=', 'cancel')]
        if exclude_ids:
            domain.append(('id', 'not in', exclude_ids))
        moves = self.env['account.move'].search(domain)
        return moves._get_taxable_gst_amounts()

    def _check_bill_limit(self, approval):
        self.ensure_one()
        # CANCEL-CONTEXT-FLAG: cancelling a bill triggers Odoo's own internal dynamic-line
        # recompute, which does a NESTED write() on this same record for other fields
        # before the state change is applied -- self.state is not reliable at that point
        # (it can still read the pre-cancel state mid-operation). A context flag, set once
        # at the top of the outer write() call, survives that nested re-entry because Odoo
        # carries context through records derived from the same environment.
        if self.env.context.get('_bafc_skip_bill_limit') or self.state == 'cancel':
            return

        moves = self.env['account.move'].search([
            ('approval_id', '=', approval.id),
            ('move_type', '!=', 'entry'),
            ('state', '!=', 'cancel'),
        ])
        booked_taxable, booked_gst = moves._get_taxable_gst_amounts()
        this_taxable, this_gst = (self if self in moves else self.browse())._get_taxable_gst_amounts()

        rounding = self.company_currency_id.rounding or 0.01
        fmt = lambda v: '{:,.2f}'.format(v)

        if float_compare(booked_taxable, approval.taxable_value, precision_rounding=rounding) > 0:
            others = booked_taxable - this_taxable
            raise UserError(_(
                "Bill limit exceeded (Taxable)!\n"
                "Approval: %(ref)s\n"
                "Approved taxable: %(approved)s\n"
                "Booked in other bills: %(others)s\n"
                "This bill: %(this)s\n"
                "Available for this bill: %(avail)s\n\n"
                "Taxable is checked against approved taxable only. TDS "
                "does not reduce the taxable amount a bill uses.",
                ref=approval.name,
                approved=fmt(approval.taxable_value),
                others=fmt(others),
                this=fmt(this_taxable),
                avail=fmt(max(approval.taxable_value - others, 0.0)),
            ))

        if float_compare(booked_gst, approval.gst, precision_rounding=rounding) > 0:
            others = booked_gst - this_gst
            raise UserError(_(
                "Bill limit exceeded (GST)!\n"
                "Approval: %(ref)s\n"
                "Approved GST: %(approved)s\n"
                "Booked in other bills: %(others)s\n"
                "This bill: %(this)s\n"
                "Available for this bill: %(avail)s\n\n"
                "GST is checked against approved GST only.",
                ref=approval.name,
                approved=fmt(approval.gst),
                others=fmt(others),
                this=fmt(this_gst),
                avail=fmt(max(approval.gst - others, 0.0)),
            ))

        self._check_bill_not_below_reserved_advance(  # ADVANCE-RESERVES-BILL
            approval, booked_taxable, booked_gst, this_taxable, this_gst)

    # ---------------- ADVANCE RESERVES BILL CAPACITY ----------------
    # ADVANCE-RESERVES-BILL: an advance payment (Advance Payment ticked) is checked
    # only against the APPROVED amount, not against any bill, because it is paid
    # before a bill exists. But once it has settled taxable or GST, that much of the
    # approved amount is no longer free for a NEW bill to claim -- otherwise the
    # approval's total exposure (bills + unmatched advances) could exceed what was
    # approved even though each side, checked alone, stays within its own limit.
    @api.model
    def _advance_reserved(self, approval):
        """(reserved_taxable, reserved_gst): taxable/GST already settled by posted
        ADVANCE payments on this approval. Matches the same per-payment TDS a payment
        itself carries (its own TDS smart-button entry + its own Other Charges TDS
        line), so an advance's own TDS is reserved along with its cash."""
        payments = self.env['account.payment'].search([
            ('approval_id', '=', approval.id), ('state', '=', 'posted'),
            ('advance_payment', '=', True),
        ])
        reserved_taxable = 0.0
        for pay in payments:
            own_tds = -sum(l.other_charge for l in pay.other_charges_lines if l.other_charge < 0)
            own_tds += pay.move_id.l10n_in_total_withholding_amount or 0.0
            reserved_taxable += pay.taxable_paid_amount + own_tds
        reserved_gst = sum(payments.mapped('gst_paid_amount'))
        return reserved_taxable, reserved_gst

    def _check_bill_not_below_reserved_advance(self, approval, booked_taxable, booked_gst, this_taxable, this_gst):

        # REVERT-TO-APPROVED-ONLY: disabled at the user's request. Bills and payments
        # are checked against the APPROVED taxable/GST amounts only, as originally
        # designed. To re-enable this cross-check later, remove this early return.
        return

        self.ensure_one()
        reserved_taxable, reserved_gst = self._advance_reserved(approval)
        rounding = self.company_currency_id.rounding or 0.01
        fmt = lambda v: '{:,.2f}'.format(v)
        billed_before_t = booked_taxable - this_taxable
        billed_before_g = booked_gst - this_gst
        if reserved_taxable > billed_before_t + 0.01:
            avail = max(approval.taxable_value - reserved_taxable - billed_before_t, 0.0)
            if float_compare(this_taxable, avail, precision_rounding=rounding) > 0:
                raise UserError(_(
                    "Bill limit exceeded (Taxable, reserved by an advance)!\n"
                    "Approval: %(ref)s\n"
                    "Approved taxable: %(approved)s\n"
                    "Already settled by advance payment(s), not yet billed: %(reserved)s\n"
                    "Booked in other bills: %(others)s\n"
                    "This bill: %(this)s\n"
                    "Available for this bill: %(avail)s\n\n"
                    "An advance payment on this approval has already used part of the approved "
                    "taxable amount. That part is reserved until a bill claims it, so it cannot "
                    "also be booked as a new bill on top.",
                    ref=approval.name, approved=fmt(approval.taxable_value),
                    reserved=fmt(reserved_taxable), others=fmt(billed_before_t),
                    this=fmt(this_taxable), avail=fmt(avail)))
        if not approval.is_reverse_charge and reserved_gst > billed_before_g + 0.01:
            avail = max(approval.gst - reserved_gst - billed_before_g, 0.0)
            if float_compare(this_gst, avail, precision_rounding=rounding) > 0:
                raise UserError(_(
                    "Bill limit exceeded (GST, reserved by an advance)!\n"
                    "Approval: %(ref)s\n"
                    "Approved GST: %(approved)s\n"
                    "Already settled by advance payment(s), not yet billed: %(reserved)s\n"
                    "Booked in other bills: %(others)s\n"
                    "This bill: %(this)s\n"
                    "Available for this bill: %(avail)s\n\n"
                    "An advance payment on this approval has already used part of the approved "
                    "GST amount. That part is reserved until a bill claims it.",
                    ref=approval.name, approved=fmt(approval.gst),
                    reserved=fmt(reserved_gst), others=fmt(billed_before_g),
                    this=fmt(this_gst), avail=fmt(avail)))

    # ---------------- EARLY TDS CHECK ON THE BILL ----------------
    # TDS is part of the taxable bucket and must be deducted once. If a bill
    # carries its own TDS line while TDS was already deducted on an earlier
    # payment (or the other way round), taxable paid + TDS goes beyond the
    # approved taxable. The final block is in _post (Confirm); these checks
    # tell the user much earlier: a warning as soon as the bill's lines change,
    # and a block when approval is requested.
    def _bill_tds_now(self):
        """TDS this bill adds through a tax line on its invoice lines
        (untaxed + GST - total). 0 once posted: it is then already in the
        approval's own figures."""
        self.ensure_one()
        if self.state == 'posted':
            return 0.0
        taxable, gst = self._get_taxable_gst_amounts()
        gross = taxable if self.approval_id.is_reverse_charge else taxable + gst
        return max(gross - self.amount_total, 0.0)

    def _bill_tds_overshoot(self, fresh=True):
        self.ensure_one()
        approval = self.approval_id
        if not approval or self.move_type != 'in_invoice' or self.state == 'posted':
            return None
        if fresh:
            pos = approval._payment_position()
            paid_taxable, tds = pos['paid_taxable'], pos['tds']
        else:
            paid_taxable, tds = approval.total_taxable_paid, approval.tds_deducted_actual
        bill_tds = self._bill_tds_now()
        return {
            'paid_taxable': paid_taxable, 'tds': tds, 'bill_tds': bill_tds,
            'over': paid_taxable + tds + bill_tds - approval.taxable_value,
        }

    def _tds_overshoot_text(self, b):
        fmt = lambda v: '{:,.2f}'.format(v)
        return _(
            "Approval: %(ref)s\n"
            "Approved taxable: %(approved)s\n"
            "Taxable already paid: %(paid)s\n"
            "TDS already deducted (payments / other bills): %(tds)s\n"
            "TDS on this bill (invoice lines): %(bill)s\n"
            "Total: %(sum)s   Excess: %(excess)s\n\n"
            "TDS is part of the taxable amount and should be deducted only once. "
            "Remove the TDS line from this bill if TDS was already deducted on "
            "the payment, or reverse/reduce the payment.",
            ref=self.approval_id.name, approved=fmt(self.approval_id.taxable_value),
            paid=fmt(b['paid_taxable']), tds=fmt(b['tds']), bill=fmt(b['bill_tds']),
            sum=fmt(b['paid_taxable'] + b['tds'] + b['bill_tds']), excess=fmt(b['over']),
        )

    def _check_limit_before_request(self):
        """Called by the Request Approval form when it opens for a bill."""
        self.ensure_one()
        b = self._bill_tds_overshoot(fresh=True)
        if b and float_compare(b['over'], 0.0, precision_rounding=0.01) > 0:
            raise UserError(_("Cannot request approval: TDS looks entered twice.\n") + self._tds_overshoot_text(b))

    @api.onchange('invoice_line_ids', 'approval_id')
    def _onchange_tds_overshoot_warning(self):
        for move in self:
            b = move._bill_tds_overshoot(fresh=False)
            if b and b['over'] > 0.01:
                return {'warning': {
                    'title': _("TDS looks entered twice"),
                    'message': _("This bill will be refused when approval is requested.\n") + move._tds_overshoot_text(b),
                }}

    # ---------------- TDS REFRESH HOOK ----------------
    # The approval's TDS Deducted / Total Payable / Remaining figures are
    # stored computed fields. The TDS total on a bill or payment entry is
    # not reliably a stored field, so posting (or cancelling) a TDS entry
    # made through the smart button never triggered a refresh and the
    # approval kept showing the old figure. Whenever a move is posted,
    # reset to draft or cancelled, find the approval it belongs to (directly,
    # or through the bill/payment a withholding entry points back to) and
    # force those figures to recompute.
    def _get_approvals_for_tds_refresh(self):
        approvals = self.env['bill.approval']
        for move in self:
            if move.approval_id:
                approvals |= move.approval_id
            # A payment's own journal entry.
            if 'payment_id' in move._fields and move.payment_id and move.payment_id.approval_id:
                approvals |= move.payment_id.approval_id
            # A TDS (withholding) entry points back, through
            # l10n_in_withholding_ref_move_id, to EITHER the bill it was
            # raised from OR the journal entry of the payment it was raised
            # from (the Odoo 17 wizard stores related_move_id or
            # related_payment_id.move_id in that one field).
            if 'l10n_in_withholding_ref_move_id' in move._fields:
                ref = move.l10n_in_withholding_ref_move_id
                if ref:
                    if ref.approval_id:
                        approvals |= ref.approval_id
                    if 'payment_id' in ref._fields and ref.payment_id and ref.payment_id.approval_id:
                        approvals |= ref.payment_id.approval_id
        return approvals

    def _refresh_approval_tds_figures(self):
        approvals = self._get_approvals_for_tds_refresh()
        if approvals:
            approvals._force_recompute_balances()

    def _post(self, soft=True):
        # CONTROL: never let the vendor be paid more than was approved, per
        # bucket (taxable vs taxable, GST vs GST; TDS belongs to the taxable
        # bucket and counts as paid) and in total. Runs on every posting
        # linked to an approval, so it covers a payment that is too large AND
        # TDS entered after payments were made (TDS entry, or a bill with a
        # TDS line), which is what pushes the taxable bucket over.
        # Anything that creates or increases an excess is blocked. An
        # approval already in excess can still receive posts that do not make
        # it worse, so corrective entries (reversals) are never trapped.
        approvals = self._get_approvals_for_tds_refresh()
        before = {a.id: a._payment_position() for a in approvals}
        posted = super()._post(soft=soft)
        fmt = lambda v: '{:,.2f}'.format(v)
        for approval in approvals:
            if approval.id not in before:
                continue
            pb = before[approval.id]
            pa = approval._payment_position()
            worse = lambda k: float_compare(pa[k], pb[k], precision_rounding=0.01) > 0
            if worse('taxable_excess'):
                raise UserError(_(
                    "Cannot post: taxable amount would be over-settled.\n"
                    "Approval: %(ref)s\n"
                    "Approved taxable: %(approved)s\n"
                    "Taxable paid: %(paid)s + TDS deducted: %(tds)s = %(sum)s\n"
                    "Excess: %(excess)s\n\n"
                    "TDS is part of the taxable amount. Record TDS before the "
                    "vendor is paid in full, or reverse/reduce the payment first.",
                    ref=approval.name, approved=fmt(approval.taxable_value),
                    paid=fmt(pa['paid_taxable']), tds=fmt(pa['tds']),
                    sum=fmt(pa['paid_taxable'] + pa['tds']),
                    excess=fmt(pa['taxable_excess']),
                ))
            if worse('gst_excess'):
                raise UserError(_(
                    "Cannot post: GST paid would exceed the approved GST.\n"
                    "Approval: %(ref)s\n"
                    "Approved GST: %(approved)s\n"
                    "GST paid: %(paid)s\n"
                    "Excess: %(excess)s",
                    ref=approval.name, approved=fmt(approval.gst),
                    paid=fmt(pa['paid_gst']), excess=fmt(pa['gst_excess']),
                ))
            if worse('total_excess'):
                raise UserError(_(
                    "Cannot post: this would leave the vendor paid more than is payable.\n"
                    "Approval: %(ref)s\n"
                    "Payable after TDS: %(cap)s\n"
                    "Already paid: %(paid)s\n"
                    "Excess: %(excess)s\n\n"
                    "Record TDS before the vendor is paid in full, or reverse "
                    "or reduce the payment first.",
                    ref=approval.name, cap=fmt(pa['cap']), paid=fmt(pa['paid']),
                    excess=fmt(pa['total_excess']),
                ))
        posted._refresh_approval_tds_figures()
        return posted

    def button_draft(self):
        res = super().button_draft()
        self._refresh_approval_tds_figures()
        return res

    def button_cancel(self):
        res = super().button_cancel()
        self._refresh_approval_tds_figures()
        return res

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)

        for rec, vals in zip(records, vals_list):
            if vals.get('approval_id'):
                approval = self.env['bill.approval'].browse(vals['approval_id'])
                rec._copy_approval_attachments(vals['approval_id'])
                rec._check_bill_limit(approval)

        return records

    def write(self, vals):
        # CANCEL-CONTEXT-FLAG: flag this on the context, not just in a local variable, so
        # nested write() re-entries triggered internally by Odoo's own dynamic-line
        # recompute during this same operation see it too (see _check_bill_limit).
        if vals.get('state') == 'cancel':
            self = self.with_context(_bafc_skip_bill_limit=True)

        # BILLED-VS-PAID: if this write cancels a bill (or bills) under an approval, make
        # sure doing so would not leave already-settled non-advance payments above what
        # remains billed. Checked BEFORE the write, against the state as it is now.
        if vals.get('state') == 'cancel':
            for approval in self.filtered(lambda m: m.approval_id).mapped('approval_id'):
                approval._check_billed_not_below_paid(exclude_bill_ids=self.ids)

        is_cancel = vals.get('state') == 'cancel'  # CANCEL-SKIPS-BILL-LIMIT
        res = super().write(vals)

        # GATE-ON-RELEVANT-FIELDS: only re-validate the bill limit when this write actually
        # changes something that can change what is BOOKED -- the bill's own lines, or which
        # approval it belongs to. Odoo's core account.move write() internally performs its own
        # nested writes as part of dynamic-line bookkeeping (and other unrelated code, such as
        # a budget-figure update on cancel, does the same) -- these touch fields that have
        # nothing to do with taxable/GST content, but without this gate they were still
        # re-triggering this check on every such internal write, sometimes mid-operation
        # (e.g. mid-cancel, before the state change was applied), causing false refusals that
        # no state/context flag could reliably catch, because the internal write could come
        # from anywhere. Gating on the actual relevant fields is correct regardless of WHY or
        # WHEN the write happened, not just for cancel.
        relevant_fields = {'invoice_line_ids', 'line_ids', 'approval_id'}
        touches_relevant = bool(relevant_fields & set(vals.keys()))
        for rec in self:
            if rec.approval_id:
                rec._copy_approval_attachments(rec.approval_id.id)
                if not is_cancel and touches_relevant:
                    rec._check_bill_limit(rec.approval_id)

        return res

class AccountPayment(models.Model):
    _inherit = 'account.payment'

    approval_id = fields.Many2one('bill.approval', string='Bill Approval')

    # GST-vs-Taxable payment split, for GST-paid reporting. The ledger
    # itself has no concept of "this rupee was GST, that rupee was
    # taxable" -- a bill's payable balance is one lump sum -- so this is
    # the only way to get a genuine "GST Paid so far" figure for
    # reporting. Auto-split proportionally (by the linked bill's own
    # taxable:GST ratio) whenever amount or the ticked invoice changes,
    # but the user can still manually edit these before saving (e.g. to
    # do a taxable-only payment now, GST-only payment later) -- the
    # onchange won't re-fire and overwrite a manual edit unless amount or
    # the ticked invoice changes again.
    gst_paid_amount = fields.Float(
        string='GST Paid', tracking=True,
        help="How much of this payment's Amount is meant to settle GST, "
             "as opposed to the taxable base. Auto-split proportionally "
             "from the linked bill's own taxable:GST ratio; edit "
             "manually for a taxable-only or GST-only payment."
    )
    taxable_paid_amount = fields.Float(
        string='Taxable Paid', tracking=True,
        help="How much of this payment's Amount is meant to settle the "
             "taxable base, as opposed to GST. Auto-split proportionally "
             "from the linked bill's own taxable:GST ratio; edit "
             "manually for a taxable-only or GST-only payment."
    )

    # NOTE: deliberately NOT a hard @api.constrains here anymore. A
    # blocking constraint on "gst_paid_amount + taxable_paid_amount must
    # equal amount" sounds safe in principle, but in practice `amount`
    # can be changed by several different code paths in sequence within
    # the same transaction (this module's own write(), the "Update
    # Amount" button, other onchange chains) and Odoo's constraint/flush
    # timing does not reliably guarantee our resync logic runs before the
    # constraint is checked. This repeatedly blocked legitimate,
    # otherwise-correct payments/approvals with no way for the user to
    # fix it except discarding and starting over. Since gst_paid_amount/
    # taxable_paid_amount only feed a REPORTING rollup (total_gst_paid /
    # total_taxable_paid on bill.approval) rather than any real ledger
    # entry, silently self-healing (see write()/create() below and
    # action_post() below) is safer than blocking real work over a
    # reporting-field mismatch.
    def _resync_gst_taxable_split(self):
        """Force gst_paid_amount/taxable_paid_amount back into sync
        with the current amount, silently. Called at write()/create()
        time when amount changes without an explicit split, and again
        defensively right before posting so a stale/mismatched split can
        never block Confirm or corrupt the reporting rollup.
        """
        for rec in self:
            if not rec.approval_id:
                continue
            if abs((rec.gst_paid_amount + rec.taxable_paid_amount) - rec.amount) <= 0.01:
                continue
            taxable_paid, gst_paid = rec._derive_split()
            super(AccountPayment, rec).write({
                'taxable_paid_amount': taxable_paid,
                'gst_paid_amount': gst_paid,
            })

    def _get_gst_taxable_ratio(self):
        """Return (net_taxable, gst) for whichever bill this payment is
        actually reconciling against (the ticked payment_invoice_ids
        line), using that BILL's OWN real figures.

        IMPORTANT: net_taxable is the bill's taxable amount MINUS that
        same bill's own TDS (tax-line TDS baked into amount_total, PLUS
        any smart-button TDS on it) -- TDS only ever comes out of the
        taxable side, never GST (matching the confirmed design: "TDS is
        from taxable, GST is only GST"). gst is the bill's full real GST,
        completely untouched by TDS.

        This MUST stay scoped to the single ticked bill, not blended with
        approval-wide totals -- mixing an approval-wide "already paid"
        figure against a single bill's gross target was the actual bug
        that caused GST Paid to be massively understated across multiple
        bills (verified: it silently let TDS eat into the GST bucket
        instead of being fully absorbed by taxable).

        Falls back to the approval's own taxable_value/gst (net of the
        estimate) if no bill is ticked yet (advance-payment phase).
        """
        self.ensure_one()
        ticked = self.payment_invoice_ids.filtered(
            lambda l: l.is_reconcile_amount or l.reconcile_amount
        )[:1]
        if ticked and ticked.invoice_id and ticked.invoice_id.move_id:
            move = ticked.invoice_id.move_id
            taxable = move.amount_untaxed
            gst_lines = move.line_ids.filtered(
                lambda l: l.tax_line_id and 'gst' in (l.tax_line_id.name or '').lower()
            )
            gst = abs(sum(gst_lines.mapped('balance')))
            # This bill's own combined TDS: tax-line TDS (the gap between
            # gross taxable+gst and the bill's real amount_total) PLUS
            # any smart-button TDS already posted on it.
            tax_line_tds = (taxable + gst) - move.amount_total
            smart_tds = move.l10n_in_total_withholding_amount
            net_taxable = taxable - tax_line_tds - smart_tds
            return net_taxable, gst
        if self.approval_id:
            return (
                self.approval_id.taxable_value - self.approval_id.tds_deducted_actual,
                self.approval_id.gst,
            )
        return 0.0, 0.0

    def _already_taxable_paid_for_bill(self):
        """Sum of taxable_paid_amount from OTHER already-posted
        payments reconciled against the SAME bill as this payment's
        ticked invoice line -- scoped per-bill, not approval-wide, for
        the same reason _get_gst_taxable_ratio is bill-scoped (see its
        docstring). Returns 0 if no bill is ticked (advance phase, where
        the approval-wide total_taxable_paid is used instead).
        """
        self.ensure_one()
        ticked = self.payment_invoice_ids.filtered(
            lambda l: l.is_reconcile_amount or l.reconcile_amount
        )[:1]
        if not (ticked and ticked.invoice_id and ticked.invoice_id.move_id):
            return None
        move = ticked.invoice_id.move_id
        other_payments = self.env['account.payment'].search([
            ('payment_invoice_ids.invoice_id.move_id', '=', move.id),
            ('state', '=', 'posted'),
        ]) - self._origin
        return sum(other_payments.mapped('taxable_paid_amount'))

    def _compute_default_gst_taxable_split(self, already_taxable_paid, amount, taxable_target, gst_target):
        """Three-tier fill: taxable first, then GST (capped at the
        bill's own real GST -- never more), then any further excess
        spills back into the taxable bucket as a genuine advance toward
        the approval's yet-unbilled remainder.

        `taxable_target` here is already NET of TDS (see
        _get_gst_taxable_ratio) and `already_taxable_paid` is scoped to
        the SAME bill this payment is settling (see
        _already_taxable_paid_for_bill) -- both bases must match, or GST
        gets silently shortchanged (confirmed bug: mixing an
        approval-wide already-paid figure against a single bill's target
        caused GST Paid to be massively understated once more than one
        bill existed).

        The third tier exists because a payment can legitimately be
        LARGER than the specific bill it's reconciling against (e.g.
        partly settling this bill, partly advancing toward a bill not
        yet raised). Without capping GST at the bill's own real GST
        amount, that excess would all get dumped into gst_paid, wildly
        overstating it beyond what this bill could possibly owe in GST
        (confirmed: an 8,00,000 payment against a 5,10,000 bill would
        otherwise report ~3,80,000 GST paid on a bill whose real GST was
        only 90,000).
        """
        remaining_taxable = max(taxable_target - already_taxable_paid, 0.0)
        if amount <= remaining_taxable:
            return amount, 0.0
        taxable_paid = remaining_taxable
        remaining_after_taxable = amount - remaining_taxable
        if remaining_after_taxable <= gst_target:
            return taxable_paid, remaining_after_taxable
        # Overflow beyond even this bill's full GST -- treat as an
        # advance toward the unbilled remainder, not extra GST.
        gst_paid = gst_target
        overflow = remaining_after_taxable - gst_target
        return taxable_paid + overflow, gst_paid

    # ---------------- SPLIT + BUCKET CONTROL ----------------
    # A payment has no GST field on its form, so unless a bill is ticked in
    # the Invoices grid nobody has said any of it is GST: the WHOLE amount
    # counts against the Taxable bucket. GST is only assigned when a bill
    # that carries GST is ticked (bill-scoped split). This stops an advance
    # payment from silently eating the GST bucket.
    def _has_ticked_bill(self):
        self.ensure_one()
        ticked = self.payment_invoice_ids.filtered(
            lambda l: l.is_reconcile_amount or l.reconcile_amount
        )[:1]
        return bool(ticked and ticked.invoice_id and ticked.invoice_id.move_id)

    def _derive_split(self, amount=None):
        """(taxable, gst) parts of `amount` (default: this payment's amount).
        GST-MANUAL: nothing is assumed to be GST. The whole amount counts as
        Taxable and the user types the GST portion themselves."""
        self.ensure_one()
        amount = self.amount if amount is None else amount
        return amount, 0.0

    def _effective_split(self):
        """The split stored on this payment if it is consistent with the
        amount, otherwise what it would be re-derived to."""
        self.ensure_one()
        if abs((self.gst_paid_amount + self.taxable_paid_amount) - self.amount) <= 0.01:
            return self.taxable_paid_amount, self.gst_paid_amount
        return self._derive_split()

    def _bucket_overshoot(self, approval, t_part, g_part, fresh=True):
        """How far this payment's taxable / GST parts would take each bucket
        beyond the approved amount. TDS is part of the taxable bucket."""
        self.ensure_one()
        tds = approval._payment_position()['tds'] if fresh else approval.tds_deducted_actual
        exclude = [self._origin.id] if self._origin.id else []
        others = self.env['account.payment'].search([
            ('approval_id', '=', approval.id),
            ('state', '=', 'posted'),
            ('id', 'not in', exclude),
        ])
        others_t = sum(others.mapped('taxable_paid_amount'))
        others_g = sum(others.mapped('gst_paid_amount'))
        taxable_left = approval.taxable_value - others_t - tds
        gst_left = 0.0 if approval.is_reverse_charge else approval.gst - others_g
        return {
            'tds': tds, 'others_t': others_t, 'others_g': others_g,
            'taxable_left': taxable_left, 'gst_left': gst_left,
            'taxable_over': t_part - taxable_left,
            'gst_over': 0.0 if approval.is_reverse_charge else g_part - gst_left,
        }

    def _check_advance_not_beyond_bill_reserved(self, approval):

        # REVERT-TO-APPROVED-ONLY: disabled at the user's request. Bills and payments
        # are checked against the APPROVED taxable/GST amounts only, as originally
        # designed. To re-enable this cross-check later, remove this early return.
        return

        """ADVANCE-VS-BILL-RESERVED: the mirror of ADVANCE-RESERVES-BILL in account.move.
        An advance payment (Advance Payment ticked) is otherwise exempt from the
        billed-vs-paid check, because it is meant for money paid before any bill exists.
        But once a bill DOES exist, that bill's own taxable (net of its own TDS) is
        reserved for its OWN payment -- an advance is not entitled to draw on it, only
        on whatever approved capacity the existing bill(s) have not already claimed.
        Ceiling = approved taxable/GST minus what is already billed (and, for taxable,
        minus that billed amount's own TDS)."""
        self.ensure_one()
        if self.payment_type != 'outbound' or not getattr(self, 'advance_payment', False):
            return
        t_part, g_part = self._effective_split()
        own_tds = -sum(l.other_charge for l in self.other_charges_lines if l.other_charge < 0)
        own_tds += self.move_id.l10n_in_total_withholding_amount or 0.0
        Move = self.env['account.move']
        billed_taxable, billed_gst = Move._billed_position(approval)
        bill_tds = approval.tds_bill_lines + approval.tds_bill_button
        exclude = [self._origin.id] if self._origin.id else []
        other_advances = self.env['account.payment'].search([
            ('approval_id', '=', approval.id), ('state', '=', 'posted'),
            ('advance_payment', '=', True), ('id', 'not in', exclude),
        ])
        other_adv_t = 0.0
        for pay in other_advances:
            pay_tds = -sum(l.other_charge for l in pay.other_charges_lines if l.other_charge < 0)
            pay_tds += pay.move_id.l10n_in_total_withholding_amount or 0.0
            other_adv_t += pay.taxable_paid_amount + pay_tds
        other_adv_g = sum(other_advances.mapped('gst_paid_amount'))
        fmt = lambda v: '{:,.2f}'.format(v)
        ceiling_t = max(approval.taxable_value - billed_taxable - bill_tds, 0.0)
        this_total_t = other_adv_t + t_part + own_tds
        if float_compare(this_total_t, ceiling_t, precision_rounding=0.01) > 0:
            raise UserError(_(
                "Cannot request approval: an existing bill already reserves this taxable amount.\n"
                "Approval: %(ref)s\n"
                "Approved taxable: %(approved)s\n"
                "Already billed, with its own TDS: %(billed)s + %(tds)s = %(billedtotal)s\n"
                "Left for an advance payment: %(ceiling)s\n"
                "Already settled by other advance payment(s): %(other)s\n"
                "This payment's taxable (incl. its own TDS): %(this)s\n\n"
                "A bill already exists on this approval and its own taxable amount is reserved for "
                "its own payment. Tie this payment to that bill in the Invoices grid instead of "
                "Advance Payment, or reduce this payment.",
                ref=approval.name, approved=fmt(approval.taxable_value),
                billed=fmt(billed_taxable), tds=fmt(bill_tds), billedtotal=fmt(billed_taxable + bill_tds),
                ceiling=fmt(ceiling_t), other=fmt(other_adv_t), this=fmt(t_part + own_tds)))
        if not approval.is_reverse_charge:
            ceiling_g = max(approval.gst - billed_gst, 0.0)
            this_total_g = other_adv_g + g_part
            if float_compare(this_total_g, ceiling_g, precision_rounding=0.01) > 0:
                raise UserError(_(
                    "Cannot request approval: an existing bill already reserves this GST amount.\n"
                    "Approval: %(ref)s\n"
                    "Approved GST: %(approved)s\n"
                    "Already billed: %(billed)s\n"
                    "Left for an advance payment: %(ceiling)s\n"
                    "Already settled by other advance payment(s): %(other)s\n"
                    "This payment's GST: %(this)s\n\n"
                    "A bill already exists on this approval and its own GST is reserved for its own "
                    "payment. Tie this payment to that bill in the Invoices grid instead of Advance "
                    "Payment, or reduce this payment's GST portion.",
                    ref=approval.name, approved=fmt(approval.gst), billed=fmt(billed_gst),
                    ceiling=fmt(ceiling_g), other=fmt(other_adv_g), this=fmt(g_part)))

    def _check_grid_matches_amount(self, approval):

        # REVERT-TO-APPROVED-ONLY: disabled at the user's request. Bills and payments
        # are checked against the APPROVED taxable/GST amounts only, as originally
        # designed. To re-enable this cross-check later, remove this early return.
        return

        """GRID-MATCHES-AMOUNT: a non-advance payment must actually be TAGGED to the
        bill(s) it is meant to settle -- ticked in the Invoices grid, for the same
        money -- not just correct in rupee terms. Without this, a payment could pass
        every taxable/GST/billed-vs-paid check while being reconciled against the
        wrong bill, a partial amount of the grid, or no bill at all (and Advance
        Payment left unticked, which is itself refused elsewhere, at Confirm, by the
        reconcile module -- this check catches the same problem earlier, at Request
        Approval, with a clearer message naming the approval)."""
        self.ensure_one()
        if self.payment_type != 'outbound' or getattr(self, 'advance_payment', False):
            return
        ticked_total = sum(
            l.reconcile_amount for l in self.payment_invoice_ids
            if l.is_reconcile_amount or l.reconcile_amount
        )
        fmt = lambda v: '{:,.2f}'.format(v)
        if float_compare(ticked_total, self.amount, precision_rounding=0.01) != 0:
            raise UserError(_(
                "Cannot request approval: this payment is not tagged to its bill(s).\n"
                "Approval: %(ref)s\n"
                "Payment amount: %(amount)s\n"
                "Reconcile Amount ticked in the Invoices grid: %(grid)s\n\n"
                "Tick the bill(s) this payment settles in the Invoices grid, so that the "
                "Reconcile Amount total equals the payment amount. Use Update Amount to pay "
                "ticked bills in full, or type each Reconcile Amount yourself for a part "
                "payment. If this payment is genuinely not against a specific bill yet, tick "
                "Advance Payment? instead.",
                ref=approval.name, amount=fmt(self.amount), grid=fmt(ticked_total)))

    def _check_payment_not_above_billed(self, approval):

        # REVERT-TO-APPROVED-ONLY: disabled at the user's request. Bills and payments
        # are checked against the APPROVED taxable/GST amounts only, as originally
        # designed. To re-enable this cross-check later, remove this early return.
        return

        """BILLED-VS-PAID: a payment that settles a specific bill (Advance Payment NOT
        ticked) may not, together with everything else already settled the same way,
        take taxable paid + TDS, or GST paid, above what this approval has actually
        BILLED so far. This is tighter than the approved-amount check: it stops a
        vendor being paid for taxable or GST that has not yet been billed at all.
        An advance payment (Advance Payment ticked) is exempt on purpose -- that is
        what an advance is -- and is governed only by the approved-amount limit."""
        self.ensure_one()
        if self.payment_type != 'outbound' or getattr(self, 'advance_payment', False):
            return
        t_part, g_part = self._effective_split()
        tds = approval._payment_position()['tds']
        exclude = [self._origin.id] if self._origin.id else []
        others = self.env['account.payment'].search([
            ('approval_id', '=', approval.id), ('state', '=', 'posted'),
            ('advance_payment', '=', False), ('id', 'not in', exclude),
        ])
        others_t = sum(others.mapped('taxable_paid_amount'))
        others_g = sum(others.mapped('gst_paid_amount'))
        billed_taxable, billed_gst = self.env['account.move']._billed_position(approval)
        fmt = lambda v: '{:,.2f}'.format(v)
        this_taxable_total = others_t + tds + t_part
        if float_compare(this_taxable_total, billed_taxable, precision_rounding=0.01) > 0:
            raise UserError(_(
                "Cannot request approval: this payment is not covered by any bill.\n"
                "Approval: %(ref)s\n"
                "Billed so far (taxable): %(billed)s\n"
                "Already settled (paid + TDS, non-advance): %(settled)s\n"
                "This payment's taxable portion: %(this)s\n"
                "Available against bills already booked: %(avail)s\n\n"
                "A bill has not yet been booked for this much taxable value. Book the bill "
                "first, or tick Advance Payment? if this is genuinely paid ahead of the bill.",
                ref=approval.name, billed=fmt(billed_taxable), settled=fmt(others_t + tds),
                this=fmt(t_part), avail=fmt(max(billed_taxable - others_t - tds, 0.0))))
        this_gst_total = others_g + g_part
        if not approval.is_reverse_charge and float_compare(this_gst_total, billed_gst, precision_rounding=0.01) > 0:
            raise UserError(_(
                "Cannot request approval: this payment's GST is not covered by any bill.\n"
                "Approval: %(ref)s\n"
                "Billed so far (GST): %(billed)s\n"
                "Already settled GST (non-advance): %(settled)s\n"
                "This payment's GST portion: %(this)s\n"
                "Available against bills already booked: %(avail)s\n\n"
                "A bill has not yet been booked for this much GST. Book the bill first, or "
                "tick Advance Payment? if this is genuinely paid ahead of the bill.",
                ref=approval.name, billed=fmt(billed_gst), settled=fmt(others_g),
                this=fmt(g_part), avail=fmt(max(billed_gst - others_g, 0.0))))

    def _check_payment_buckets(self, approval):
        """Taxable to taxable, GST to GST: this payment's own parts against
        the approved buckets (TDS already deducted counts in taxable)."""
        self.ensure_one()
        if self.payment_type != 'outbound':
            return
        t_part, g_part = self._effective_split()
        b = self._bucket_overshoot(approval, t_part, g_part)
        fmt = lambda v: '{:,.2f}'.format(v)
        if float_compare(b['taxable_over'], 0.0, precision_rounding=0.01) > 0:
            no_gst = _(
                "This payment has no GST portion, so its whole amount counts "
                "against the approved taxable amount.\n") if not g_part else ""
            raise UserError(_(
                "Payment limit exceeded (Taxable)!\n"
                "Approval: %(ref)s\n"
                "Approved taxable: %(approved)s\n"
                "Taxable already paid: %(paid)s\n"
                "TDS deducted (part of taxable): %(tds)s\n"
                "Taxable left for this payment: %(left)s\n"
                "This payment counts as taxable: %(this)s\n\n"
                "%(no_gst)sReduce this payment to %(allowed)s or less.",
                ref=approval.name, approved=fmt(approval.taxable_value),
                paid=fmt(b['others_t']), tds=fmt(b['tds']),
                left=fmt(max(b['taxable_left'], 0.0)), this=fmt(t_part),
                no_gst=no_gst,
                allowed=fmt(max(self.amount - b['taxable_over'], 0.0)),
            ))
        if float_compare(b['gst_over'], 0.0, precision_rounding=0.01) > 0:
            raise UserError(_(
                "Payment limit exceeded (GST)!\n"
                "Approval: %(ref)s\n"
                "Approved GST: %(approved)s\n"
                "GST already paid: %(paid)s\n"
                "This payment's GST portion: %(this)s\n\n"
                "GST is checked against approved GST only.",
                ref=approval.name, approved=fmt(approval.gst),
                paid=fmt(b['others_g']), this=fmt(g_part),
            ))

    @api.onchange('gst_paid_amount')
    def _onchange_gst_paid_amount(self):
        """The user edits the GST portion; the taxable portion is whatever
        is left of the amount."""  # GST-FIX
        for rec in self:
            if not rec.approval_id:
                continue
            gst = max(rec.gst_paid_amount or 0.0, 0.0)
            # AMOUNT-FROM-GST: GST typed while the Amount is still empty (a GST-only payment):
            # the Amount is the same figure. Not for Other Charges payments, whose Amount is
            # calculated from the base amount and the charge lines.
            if not rec.amount and gst and not getattr(rec, 'other_charge_applicable', False):
                rec.amount = gst
                rec.taxable_paid_amount = 0.0
                rec.gst_paid_amount = gst
                continue
            # Inside this onchange the Amount can read as 0 even though the form
            # shows it, so it must NOT be trusted to cap the GST (that is what reset
            # the GST portion to 0). Fall back to the split already saved, and only
            # cap when an amount is really known. The stored numbers are recomputed
            # from the real amount on save (see create / write).
            amount = rec.amount or (rec._origin.taxable_paid_amount + rec._origin.gst_paid_amount)
            if amount:
                gst = min(gst, amount)
                rec.taxable_paid_amount = amount - gst
            rec.gst_paid_amount = gst

    @api.onchange('amount', 'payment_invoice_ids')
    def _onchange_amount_split_gst(self):
        for rec in self:
            if not rec.approval_id:
                continue
            # A GST portion that already adds up with the taxable portion to the Amount is
            # what the user typed (or was just filled in from it): keep it. Any other change
            # of the Amount starts from "everything is taxable" again.
            if rec.gst_paid_amount and abs(rec.taxable_paid_amount + rec.gst_paid_amount - rec.amount) <= 0.01:
                continue
            taxable_paid, gst_paid = rec._derive_split()
            rec.taxable_paid_amount = taxable_paid
            rec.gst_paid_amount = gst_paid

    @api.onchange('amount', 'approval_id', 'gst_paid_amount')
    def _onchange_amount_approval_limit_warning(self):
        for rec in self:
            approval = rec.approval_id
            if not approval or rec.payment_type != 'outbound' or not rec.amount:
                continue
            # WARN-NOT-ON-OPEN: do not pop a warning while the form is still showing
            # the amount pre-filled by the Payment button (default_amount): a dialog
            # raised during the opening of the form can leave the page stuck. The
            # warning starts once the user changes the amount or the GST portion.
            default_amount = self.env.context.get('default_amount')
            if (default_amount is not None
                    and float_compare(rec.amount, default_amount, precision_rounding=0.01) == 0):
                continue
            t_part, g_part = rec._effective_split()
            b = rec._bucket_overshoot(approval, t_part, g_part, fresh=False)
            if b['taxable_over'] > 0.01 or b['gst_over'] > 0.01:
                fmt = lambda v: '{:,.2f}'.format(v)
                return {'warning': {
                    'title': _("Amount is above what is payable"),
                    'message': _(
                        "Approval %(ref)s: this payment counts %(t)s against "
                        "Taxable (%(tl)s left after TDS and payments made) and "
                        "%(g)s against GST (%(gl)s left). A payment with no "
                        "GST portion counts entirely as Taxable. It will be "
                        "rejected when you request approval.",
                        ref=approval.name, t=fmt(t_part), tl=fmt(max(b['taxable_left'], 0.0)),
                        g=fmt(g_part), gl=fmt(max(b['gst_left'], 0.0)),
                    ),
                }}

    def _copy_approval_attachments(self, approval_id):
        if not approval_id:
            return

        attachments = self.env['ir.attachment'].search([
            ('res_model', '=', 'bill.approval'),
            ('res_id', '=', approval_id)
        ])

        for att in attachments:
            existing = self.env['ir.attachment'].search([
                ('res_model', '=', 'account.payment'),
                ('res_id', '=', self.id),
                ('name', '=', att.name),
            ], limit=1)

            if not existing:
                att.copy({
                    'res_model': 'account.payment',
                    'res_id': self.id,
                })

    def _sync_budget_from_bill(self, approval):
        """Copy the Budget (crossovered_budget) from this approval's
        bill onto the payment's own journal entry (move_id).

        crossovered_budget lives on account.move and is normally only
        set via _onchange_date_update_budget (accounts_extended), which
        picks a budget purely by date range on the move's own Accounting
        Date -- it has no awareness of which bill a payment is actually
        settling. Left alone, a payment's move can end up with no budget
        at all (blocking Confirm with "Kindly select a Budget"), or with
        a budget that doesn't match the bill it's paying against.

        Instead, inherit the budget directly from the linked bill: the
        payment should always be attributed to the same budget as the
        bill it's settling, not whatever the date-based lookup guesses.
        Only sets it if not already set, so a manually-chosen budget on
        the payment's move is never silently overwritten.
        """
        self.ensure_one()
        if not self.move_id or self.move_id.crossovered_budget:
            return
        if not approval or not approval.account_move_ids:
            return
        bill_with_budget = approval.account_move_ids.filtered(
            lambda m: m.move_type != 'entry' and m.crossovered_budget
        )[:1]
        if bill_with_budget:
            self.move_id.crossovered_budget = bill_with_budget.crossovered_budget.id

    # ---------------- PAYMENT LIMIT CHECK ----------------
    def _get_approval_tds_withheld(self, approval):
        """Total TDS/withholding already deducted against this approval's
        bill(s), via Odoo's l10n_in_withholding "TDS" smart button.

        That flow creates a *separate* account.move (the withholding move,
        linked back to the original bill via l10n_in_withholding_ref_move_id)
        instead of reducing the original bill's amount_total. Because that
        withholding move is never itself linked via approval_id, it's
        invisible to a plain sum of moves/payments on this approval unless
        we explicitly pull it in here.

        TDS entered directly as a tax line on the bill's own invoice lines
        is NOT handled here on purpose: that kind of TDS already reduces
        the bill's own amount_total, so it's already correctly reflected
        by _check_bill_limit()/amount_total and must not be subtracted a
        second time here.

        Coded defensively: l10n_in_withholding is an optional India
        localization module, so we check the field exists before using it
        rather than adding a hard dependency on it.
        """
        moves = self.env['account.move'].search([
            ('approval_id', '=', approval.id), ('move_type', '!=', 'entry'),
        ])
        if 'l10n_in_total_withholding_amount' not in moves._fields:
            return 0.0
        # l10n_in_total_withholding_amount already only sums *posted*
        # withholding entries, so draft/unconfirmed TDS wizards don't
        # prematurely eat into the available payment headroom.
        return sum(moves.mapped('l10n_in_total_withholding_amount'))

    def _check_payment_limit(self, approval):
        self.ensure_one()

        # Only enforce the cap once the payment is actually being
        # finalized (posted). While a payment is still in 'draft', the UI
        # legitimately passes through intermediate/incorrect amounts --
        # e.g. the "Invoices" reconciliation grid (partial_payment_adjustment
        # module) auto-saves the record's current on-screen amount BEFORE
        # its own "Update Amount" button logic runs and corrects it. If we
        # enforce the cap on every draft write, that auto-save trips this
        # check with a transient, not-yet-corrected amount and blocks the
        # user from ever reaching the correct figure. The real, authoritative
        # enforcement now happens in action_post() below.
        if self.state in ('draft', 'cancel'):
            return

        # IMPORTANT: only count payments that are already POSTED, plus
        # self (which is mid-action_post() when this runs). Other unrelated
        # DRAFT payments on the same approval must NOT count towards the
        # cap here -- otherwise posting a single valid payment could be
        # incorrectly blocked just because an unrelated/unfinished draft
        # happens to exist alongside it. Drafts are allowed to coexist
        # (needed for the reconciliation-grid UX above); only actual
        # postings are financially real and must be capped.
        posted_payments = self.env['account.payment'].search([
            ('approval_id', '=', approval.id),
            ('state', '=', 'posted'),
        ])
        total = sum(posted_payments.mapped('amount'))
        if self.id not in posted_payments.ids:
            total += self.amount

        # The cap is ALWAYS measured against the full APPROVED amount --
        # booking a bill (even a partial one) must never shrink the
        # ceiling on its own. Only actual POSTED PAYMENTS reduce
        # available headroom (via `total` below); TDS is netted off using
        # the best figure currently known (the real, combined TDS across
        # whatever's been billed so far if any bill exists, else the
        # approval-time estimate) -- but the BASE stays the full approved
        # taxable+gst throughout, so advance payments can always proceed
        # up to the full approved amount regardless of how much has (or
        # hasn't) been billed yet.
        # Single definition of the ceiling lives on the approval
        # (bill.approval._get_payment_cap) so this check and the
        # "Balance Payable / Excess Paid" columns can never disagree:
        # approved taxable (+ GST unless RCM) minus the TDS known so far,
        # from whichever of the three places it was entered.
        cap = approval._get_payment_cap()

        # Taxable to taxable, GST to GST first (most specific message), then
        # the overall ceiling.
        # Only while the payment is still waiting to be posted: once posted,
        # the post-time guard in account.move._post has already ruled on it,
        # and re-testing a posted (possibly older) payment on every write
        # would trap corrective actions on approvals that are already over.
        if self.state != 'posted':
            self._check_payment_buckets(approval)
            self._check_payment_not_above_billed(approval)  # BILLED-VS-PAID
            self._check_advance_not_beyond_bill_reserved(approval)  # ADVANCE-VS-BILL-RESERVED
            self._check_grid_matches_amount(approval)  # GRID-MATCHES-AMOUNT

        if float_compare(total, cap, precision_rounding=0.01) > 0:
            self._raise_payment_limit_error(cap, total - self.amount)

    def _check_charge_line_accounts(self, commands):
        """CHARGE-ACCOUNT-CHECK: an Other Charges line must post to a usable account.
        A tax with no account is silently skipped when the entry is built, so the
        deduction would reduce the payment but never reach the books; a deprecated
        account cannot be posted at all. Refuse both at save, saying which tax."""
        Tax = self.env['account.tax']
        Acc = self.env['account.account']
        for cmd in commands or []:
            if not (isinstance(cmd, (list, tuple)) and len(cmd) == 3 and cmd[0] in (0, 1)
                    and isinstance(cmd[2], dict)):
                continue
            lv = cmd[2]
            if not lv.get('tax_id'):
                continue
            tax = Tax.browse(lv['tax_id'])
            acc = Acc.browse(lv['account_id']) if lv.get('account_id') else tax.invoice_repartition_line_ids.filtered(
                lambda l: l.account_id and l.repartition_type != 'base')[:1].account_id
            if not acc:
                raise UserError(_(
                    "The tax %s has no account to post to, so it cannot be used in Other Charges. "
                    "Choose another tax, or set an account on the tax's repartition lines.") % tax.display_name)
            if getattr(acc, 'deprecated', False):
                raise UserError(_(
                    "The tax %(tax)s posts to the account %(acc)s, which is deprecated, so this payment "
                    "cannot be posted. Choose another tax, or replace the account on the tax.") % {
                        'tax': tax.display_name, 'acc': acc.display_name})

    def _raise_payment_limit_error(self, cap, others):
        """One message for every place the payment limit is enforced."""
        fmt = lambda v: '{:,.2f}'.format(v)
        raise UserError(_(
            "Payment limit exceeded!\n"
            "Payable for this approval (after GST-RCM / TDS): %(cap)s\n"
            "Already paid (posted payments): %(others)s\n"
            "This payment: %(this)s\n"
            "Excess: %(excess)s\n\n"
            "Reduce this payment to %(allowed)s or less.",
            cap=fmt(cap), others=fmt(others), this=fmt(self.amount),
            excess=fmt(others + self.amount - cap),
            allowed=fmt(max(cap - others, 0.0)),
        ))

    def _check_limit_before_request(self):
        """Called by the Request Approval form (request.approval.default_get)
        the moment it opens for a payment, so an over-limit payment is
        stopped BEFORE the user fills in and submits the request. Same limit
        as at submit / confirm (fresh TDS, posted payments only)."""
        self.ensure_one()
        approval = self.approval_id
        if not approval or self.payment_type != 'outbound':
            return
        if self.id and self.state != 'posted' and float_compare(self.amount, 0.0, precision_rounding=0.01) <= 0:
            raise UserError(_(
                "Enter the payment amount before requesting approval. The GST portion is part of "
                "the amount, not on top of it: to pay only the GST, set both the Amount and the "
                "GST portion to the GST figure."))
        pos = approval._payment_position()
        others = pos['paid']
        if self.id and self.state == 'posted':
            return
        self._check_payment_buckets(approval)
        self._check_payment_not_above_billed(approval)  # BILLED-VS-PAID
        self._check_advance_not_beyond_bill_reserved(approval)  # ADVANCE-VS-BILL-RESERVED
        self._check_grid_matches_amount(approval)  # GRID-MATCHES-AMOUNT
        if float_compare(others + self.amount, pos['cap'], precision_rounding=0.01) > 0:
            self._raise_payment_limit_error(pos['cap'], others)

    def _check_no_redundant_payment_on_create(self, approval, new_vals):
        """Blocks creating a brand-new payment outright once existing
        payment(s) already linked to this approval -- draft or posted --
        already cover the outstanding balance on its posted bill(s).

        This is safe from the "Update Amount" false-block issue (see
        _check_payment_limit above) because it only ever looks at OTHER,
        already-existing payment records' amounts -- never at the amount
        of the record currently being created. The reconciliation-grid
        flow's transient wrong-then-corrected value only ever affects a
        record's OWN amount at its OWN first save, so checking against
        already-settled prior records never trips on that.

        If there are no posted bills yet at all, there's nothing to be
        "already covered" against, so creation is allowed through (e.g.
        advance payments made before any bill exists).
        """
        bills = self.env['account.move'].search([
            ('approval_id', '=', approval.id),
            ('move_type', '!=', 'entry'),
            ('state', '=', 'posted'),
        ])
        if not bills:
            return

        outstanding = sum(bills.mapped('amount_residual'))
        if outstanding <= 0:
            raise UserError(_(
                "This approval is already fully settled -- the linked "
                "bill(s) have no outstanding balance. No further payment "
                "entries can be created against it."
            ))

        # NOTE: deliberately NOT also comparing against other existing
        # DRAFT payments' amounts here. That was tried and reverted --
        # draft account.payment records can appear (and even transiently
        # duplicate) purely as a side effect of normal UI interaction
        # (Odoo auto-saves an in-progress record the moment any button is
        # clicked, e.g. ticking a checkbox in the Invoices reconciliation
        # grid), well before the user has actually finished or intended to
        # finalize anything. A check based on draft-state data is
        # therefore unreliable and produces false positives ("a payment
        # has already been recorded" when nothing was ever really
        # recorded/finished). The `outstanding <= 0` check above is safe
        # because it only reacts to genuinely POSTED bill state.
        # Preventing a duplicate PAYMENT specifically is instead handled
        # at action_post() time by _check_payment_limit, which correctly
        # counts only posted payments (+ self) -- coexisting drafts are
        # harmless until one is actually posted.

    @api.model_create_multi
    def create(self, vals_list):
        # Auto-fill the GST/Taxable split BEFORE create(), not just via
        # the client-side onchange -- the onchange only fires on an
        # actual UI field-change event, but Amount is often just a
        # pre-filled context default (e.g. action_open_payment's
        # default_amount) that the user never manually edits. In that
        # case the onchange never runs, gst_paid_amount/taxable_paid_amount
        # stay at 0, and _check_gst_taxable_split then rejects the very
        # first save. Computing the split here guarantees it's correct
        # regardless of what happened (or didn't) in the browser.
        for vals in vals_list:
            if vals.get('approval_id') and vals.get('amount') and not (
                vals.get('gst_paid_amount') or vals.get('taxable_paid_amount')
            ):
                # No bill can be ticked yet at create() time, and a payment has
                # no GST field, so the whole amount counts as taxable. If a
                # bill is ticked later, the onchange / write() resync assigns
                # the bill's GST portion.
                vals['taxable_paid_amount'] = vals['amount']
                vals['gst_paid_amount'] = 0.0
        for vals in vals_list:
            if vals.get('other_charges_lines'):
                self._check_charge_line_accounts(vals['other_charges_lines'])
        # OTHER-CHARGES-AMOUNT: with Other Charges the payment Amount is the base amount plus the
        # charge lines (TDS is negative). The form does not always update the Amount when a line is
        # added, and a mismatch makes the journal entry get rebuilt twice (two bank lines, which
        # Odoo refuses). So take the charge amounts the form sends and set the Amount from them.
        for vals in vals_list:
            if vals.get('other_charge_applicable') and vals.get('payment_base_amount') and vals.get('other_charges_lines'):
                charges = []
                for cmd in vals['other_charges_lines']:
                    if (isinstance(cmd, (list, tuple)) and len(cmd) == 3 and cmd[0] == 0
                            and isinstance(cmd[2], dict) and 'other_charge' in cmd[2]):
                        charges.append(cmd[2]['other_charge'] or 0.0)
                    else:
                        charges = None
                        break
                if charges is not None:
                    expected = vals['payment_base_amount'] + sum(charges)
                    if float_compare(vals.get('amount') or 0.0, expected, precision_rounding=0.01) != 0:
                        vals['amount'] = expected
        # GST-FIX: when a GST portion is given, the stored taxable portion is always
        # amount - GST, using the real amount, whatever the screen sent.
        for vals in vals_list:
            if vals.get('approval_id') and vals.get('gst_paid_amount'):  # ZERO-AMOUNT-CHECK
                if float_compare(vals['gst_paid_amount'], vals.get('amount') or 0.0, precision_rounding=0.01) > 0:
                    raise UserError(_("The GST portion cannot be more than the payment amount. The GST portion is part of the "
                      "amount, not on top of it: to pay only the GST, set both the Amount and the GST "
                      "portion to the GST figure."))
                vals['taxable_paid_amount'] = (vals.get('amount') or 0.0) - vals['gst_paid_amount']

        records = super().create(vals_list)

        for rec, vals in zip(records, vals_list):
            if vals.get('approval_id'):
                approval = self.env['bill.approval'].browse(vals['approval_id'])
                rec._check_no_redundant_payment_on_create(approval, vals)
                rec._copy_approval_attachments(vals['approval_id'])
                rec._sync_budget_from_bill(approval)
                rec._check_payment_limit(approval)

        return records

    def write(self, vals):
        # If 'amount' is changing here but the caller did NOT also
        # explicitly set gst_paid_amount/taxable_paid_amount in this same
        # write, re-derive the split automatically afterwards. This is
        # necessary because `amount` can legitimately be changed by code
        # we don't control at all -- e.g. partial_payment_adjustment's
        # "Update Amount" button, which recalculates `amount` from
        # multiple ticked invoices with no knowledge that our split
        # fields exist. Without this, the split silently goes stale and
        # trips _check_gst_taxable_split's constraint on save, even
        # though the user did nothing wrong.
        resync_split = 'amount' in vals and not (
            'gst_paid_amount' in vals or 'taxable_paid_amount' in vals
        )

        res = super().write(vals)

        for rec in self:
            if rec.approval_id:
                rec._copy_approval_attachments(rec.approval_id.id)
                rec._sync_budget_from_bill(rec.approval_id)
                if resync_split:
                    taxable_paid, gst_paid = rec._derive_split()
                    super(AccountPayment, rec).write({
                        'taxable_paid_amount': taxable_paid,
                        'gst_paid_amount': gst_paid,
                    })
                if 'other_charges_lines' in vals:
                    rec._check_charge_line_accounts(vals['other_charges_lines'])
                if 'gst_paid_amount' in vals and not resync_split:
                    # GST-FIX: stored taxable = real amount - GST typed.
                    if float_compare(rec.gst_paid_amount, rec.amount, precision_rounding=0.01) > 0:
                        raise UserError(_("The GST portion cannot be more than the payment amount. The GST portion is part of the "
                      "amount, not on top of it: to pay only the GST, set both the Amount and the GST "
                      "portion to the GST figure."))
                    super(AccountPayment, rec).write({
                        'taxable_paid_amount': rec.amount - rec.gst_paid_amount,
                    })
                rec._check_payment_limit(rec.approval_id)

        return res

    def action_post(self):
        # This is the authoritative enforcement point: by the time a
        # payment is posted, any in-progress reconciliation-grid editing
        # (ticking invoices, "Update Amount", etc.) is finished and
        # rec.amount reflects the user's final intent. Block posting here
        # rather than relying on draft-time writes, which can fire mid-edit
        # with a transient amount (see _check_payment_limit above).
        self._resync_gst_taxable_split()
        for rec in self:
            if rec.approval_id:
                rec._check_payment_limit(rec.approval_id)
        return super().action_post()


# NO-PAYMENT-OUTSIDE-APPROVAL
# A vendor bill that belongs to an approval is paid ONLY from that approval (its Payment
# button). The standard "Register Payment" wizard creates a payment with no approval on it, so
# none of the approval's limits would see it, and an advance that has not been matched to the
# bill yet leaves the bill open to be paid a second time. Refuse it, and say where to pay.
class AccountPaymentRegister(models.TransientModel):
    _inherit = 'account.payment.register'

    def action_create_payments(self):
        for wizard in self:
            bills = wizard.line_ids.move_id.filtered(lambda m: m.move_type == 'in_invoice' and m.approval_id)
            if bills:
                raise UserError(_(
                    "Bill %(bills)s belongs to approval %(approvals)s, so it cannot be paid from here.\n"
                    "Open the approval and use its Payment button. Every payment must be made from the "
                    "approval, so that it is checked against the approved taxable and GST amounts.",
                    bills=', '.join(bills.mapped('name')),
                    approvals=', '.join(bills.mapped('approval_id.name'))))
        return super().action_create_payments()
