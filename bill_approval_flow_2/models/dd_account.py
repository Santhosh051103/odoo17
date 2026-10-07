from odoo import models, api, _,fields
from odoo.exceptions import UserError
from odoo.tools import float_compare


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

    def _check_bill_limit(self, approval):
        self.ensure_one()

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
        # CONTROL: never let the vendor be paid more than is payable after
        # TDS. Posting a payment is already capped by _check_payment_limit;
        # this covers the other direction: posting a TDS entry, or a bill
        # carrying TDS, AFTER payments were made, which lowers the ceiling
        # below what has already been paid. Anything that would create (or
        # increase) an excess is blocked. An approval that is already in
        # excess can still receive posts that do not make it worse, so
        # corrective entries (reversals, adjustments) are never trapped.
        approvals = self._get_approvals_for_tds_refresh()
        before = {a.id: a._payment_position() for a in approvals}
        posted = super()._post(soft=soft)
        for approval in approvals:
            if approval.id not in before:
                continue
            paid_b, cap_b = before[approval.id]
            paid_a, cap_a = approval._payment_position()
            excess_b = max(paid_b - cap_b, 0.0)
            excess_a = max(paid_a - cap_a, 0.0)
            if float_compare(excess_a, excess_b, precision_rounding=0.01) > 0:
                fmt = lambda v: '{:,.2f}'.format(v)
                raise UserError(_(
                    "Cannot post: this would leave the vendor paid more than is payable.\n"
                    "Approval: %(ref)s\n"
                    "Payable after TDS: %(cap)s\n"
                    "Already paid: %(paid)s\n"
                    "Excess: %(excess)s\n\n"
                    "TDS has to be recorded before the vendor is paid in full. "
                    "Reverse or reduce the payment first, or correct the TDS amount.",
                    ref=approval.name, cap=fmt(cap_a), paid=fmt(paid_a),
                    excess=fmt(excess_a),
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
        res = super().write(vals)

        for rec in self:
            if rec.approval_id:
                rec._copy_approval_attachments(rec.approval_id.id)
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
            taxable, gst = rec._get_gst_taxable_ratio()
            already_paid = rec._already_taxable_paid_for_bill()
            if already_paid is None:
                already_paid = rec.approval_id.total_taxable_paid
            taxable_paid, gst_paid = rec._compute_default_gst_taxable_split(
                already_paid, rec.amount, taxable, gst
            )
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

    @api.onchange('amount', 'payment_invoice_ids')
    def _onchange_amount_split_gst(self):
        for rec in self:
            if not rec.approval_id:
                continue
            taxable, gst = rec._get_gst_taxable_ratio()
            already_paid = rec._already_taxable_paid_for_bill()
            if already_paid is None:
                already_paid = rec.approval_id.total_taxable_paid
            taxable_paid, gst_paid = rec._compute_default_gst_taxable_split(
                already_paid, rec.amount, taxable, gst
            )
            rec.taxable_paid_amount = taxable_paid
            rec.gst_paid_amount = gst_paid

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
        if self.state == 'draft':
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

        if total > cap:
            raise UserError(_(
                "Payment limit exceeded!\nAllowed (net of GST-RCM / TDS already withheld): %s\nUsed (posted payments only): %s"
            ) % (cap, total))

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
                approval = self.env['bill.approval'].browse(vals['approval_id'])
                # Same fill-taxable-first-then-GST logic as the onchange
                # (see _compute_default_gst_taxable_split) -- using the
                # approval's own taxable_value/gst here since at create()
                # time we don't yet have a real record to check which
                # bill (if any) is ticked in payment_invoice_ids. This is
                # the advance-phase path; once a specific bill exists,
                # write()'s resync (via the onchange-equivalent logic)
                # takes over with bill-scoped figures instead.
                taxable_paid, gst_paid = self.env['account.payment']._compute_default_gst_taxable_split(
                    approval.total_taxable_paid, vals['amount'],
                    approval.taxable_value - approval.tds_deducted_actual, approval.gst
                )
                vals['taxable_paid_amount'] = taxable_paid
                vals['gst_paid_amount'] = gst_paid

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
                    taxable, gst = rec._get_gst_taxable_ratio()
                    already_paid = rec._already_taxable_paid_for_bill()
                    if already_paid is None:
                        already_paid = rec.approval_id.total_taxable_paid
                    taxable_paid, gst_paid = rec._compute_default_gst_taxable_split(
                        already_paid, rec.amount, taxable, gst
                    )
                    super(AccountPayment, rec).write({
                        'taxable_paid_amount': taxable_paid,
                        'gst_paid_amount': gst_paid,
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
