# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# Minimum Earned Leave balance an employee must have to be eligible
# to submit an encashment request at all.
MIN_ELIGIBLE_EL = 21.0

# Name of the Leave Type this module treats as "Earned Leave".
# Change here if your Time Off > Configuration > Leave Types uses a
# different label.
EARNED_LEAVE_TYPE_NAME = 'Earned Leave'

# hr.payslip.input.type code used to hand the approved day-count over to
# payroll. We only ever pass the raw number of days here -- we deliberately
# do NOT calculate a rupee amount ourselves (that depends on your salary
# structure's per-day rate, which lives in your payroll rules, not here).
# If you already have an existing input type for encashment, tell me its
# code and I'll point ENCASHMENT_INPUT_CODE at that instead of creating a
# new one.
ENCASHMENT_INPUT_CODE = 'ENCASH'


class HrLeaveEncashment(models.Model):
    _name = 'hr.leave.encashment'
    _description = 'Leave Encashment Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'
    _rec_name = 'name_seq'

    name_seq = fields.Char(
        string='Reference', copy=False, readonly=True, default=lambda self: _('New'))

    # --- Employee identity (auto-populated, read-only) ---------------
    employee_id = fields.Many2one(
        'hr.employee', string='Employee', required=True,
        default=lambda self: self._default_employee(), tracking=True)
    employee_name = fields.Char(related='employee_id.name', string='Name', store=True, readonly=True)
    employee_code = fields.Char(related='employee_id.employee_number', string='Employee ID', readonly=True)
    work_email = fields.Char(related='employee_id.work_email', string='Email', readonly=True)
    manager_id = fields.Many2one(
        related='employee_id.parent_id', string='Reporting Manager', store=True, readonly=True)
    has_manager = fields.Boolean(compute='_compute_has_manager')
    is_manager_viewer = fields.Boolean(compute='_compute_is_manager_viewer')
    is_hr_officer = fields.Boolean(compute='_compute_is_hr_officer')

    # --- Leave balance / request -------------------------------------
    leave_type_id = fields.Many2one(
        'hr.leave.type', string='Leave Type', readonly=True,
        default=lambda self: self._default_leave_type())
    available_el = fields.Float(
        string='Available Earned Leave', compute='_compute_available_el',
        digits=(16, 2), help='Current unused Earned Leave balance for this employee.')
    is_eligible = fields.Boolean(
        string='Eligible', compute='_compute_available_el',
        help='True only when Available Earned Leave is 21 days or more.')
    days_to_encash = fields.Float(
        string='Number of Leaves to Encash', digits=(16, 2), tracking=True,
        help='Employee may request fewer than the full available balance, '
             'but never more than what is currently available.')

    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),                 # waiting on Reporting Manager
        ('manager_approved', 'Manager Approved'),    # waiting on HR
        ('approved', 'Approved'),                    # HR-final, sent to payroll
        ('refused', 'Refused'),
    ], string='Status', default='draft', tracking=True, copy=False)

    manager_approved_by = fields.Many2one('res.users', string='Approved by (Manager)', readonly=True, copy=False)
    manager_approved_date = fields.Datetime(string='Manager Approval Date', readonly=True, copy=False)
    hr_approved_by = fields.Many2one('res.users', string='Approved by (HR)', readonly=True, copy=False)
    hr_approved_date = fields.Datetime(string='HR Approval Date', readonly=True, copy=False)
    refusal_reason = fields.Text(string='Refusal / Remarks')

    payroll_processed = fields.Boolean(
        string='Sent to Payroll', readonly=True, copy=False,
        help='Ticked once this request has been picked up as a payslip input. '
             'If unticked after HR approval, no matching draft payslip was found '
             '-- check the chatter for details and add the input manually.')
    payslip_input_id = fields.Many2one(
        'hr.payslip.input', string='Payslip Input', readonly=True, copy=False)

    leave_deducted = fields.Boolean(
        string='Balance Deducted', readonly=True, copy=False,
        help='Ticked once the encashed days have been recorded as taken '
             'Earned Leave, reducing the employee\'s available balance.')
    deduction_leave_id = fields.Many2one(
        'hr.leave', string='Deduction Time Off Entry', readonly=True, copy=False,
        help='The validated Time Off record created to reduce the Earned '
             'Leave balance by the encashed amount.')
    deduction_summary = fields.Char(
        string='Deduction Reference', compute='_compute_deduction_summary',
        help='Plain-text summary, independent of the linked Time Off '
             'record\'s own auto-generated (date-based) display name.')

    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company, required=True)

    # -------------------------------------------------------------
    # Defaults
    # -------------------------------------------------------------
    def _default_employee(self):
        # Respects default_employee_id passed via context (e.g. the
        # "Encashment" button on the Employee form itself), otherwise
        # falls back to the logged-in user's own employee record.
        employee_id = self.env.context.get('default_employee_id')
        if employee_id:
            return self.env['hr.employee'].browse(employee_id)
        return self.env.user.employee_id

    def _default_leave_type(self):
        return self.env['hr.leave.type'].search(
            [('name', '=', EARNED_LEAVE_TYPE_NAME)], limit=1)

    # -------------------------------------------------------------
    # Compute
    # -------------------------------------------------------------
    @api.depends('employee_id', 'leave_type_id')
    def _compute_available_el(self):
        for rec in self:
            balance = 0.0
            if rec.employee_id and rec.leave_type_id:
                # Standard Odoo pattern: hr.leave.type's remaining-leave
                # fields are computed per-employee via context.
                leave_type = rec.leave_type_id.with_context(
                    employee_id=rec.employee_id.id)
                balance = leave_type.virtual_remaining_leaves \
                    or leave_type.remaining_leaves or 0.0
            rec.available_el = balance
            rec.is_eligible = balance >= MIN_ELIGIBLE_EL

    @api.depends('employee_id.parent_id')
    def _compute_has_manager(self):
        for rec in self:
            rec.has_manager = bool(rec.employee_id.parent_id)

    def _compute_is_manager_viewer(self):
        # Not stored / no meaningful @api.depends target -- depends on the
        # current user, which is recomputed fresh per request anyway.
        for rec in self:
            rec.is_manager_viewer = bool(
                rec.employee_id.parent_id and rec.employee_id.parent_id.user_id == self.env.user)

    def _compute_is_hr_officer(self):
        for rec in self:
            rec.is_hr_officer = self.env.user.has_group('hr_holidays.group_hr_holidays_user')

    @api.depends('deduction_leave_id', 'days_to_encash')
    def _compute_deduction_summary(self):
        for rec in self:
            if rec.deduction_leave_id:
                rec.deduction_summary = _('%s Leave(s) - Encashment') % rec.days_to_encash
            else:
                rec.deduction_summary = False

    # -------------------------------------------------------------
    # Constraints
    # -------------------------------------------------------------
    @api.constrains('days_to_encash', 'available_el', 'employee_id')
    def _check_days_to_encash(self):
        for rec in self:
            if rec.state == 'draft':
                # Allow a blank/zero value while still drafting.
                continue
            if rec.days_to_encash <= 0:
                raise ValidationError(_('Number of leaves to encash must be greater than zero.'))
            if rec.days_to_encash > rec.available_el:
                raise ValidationError(_(
                    'You cannot encash more days (%(req)s) than your current '
                    'Earned Leave balance (%(bal)s).'
                ) % {'req': rec.days_to_encash, 'bal': rec.available_el})

    @api.constrains('employee_id', 'leave_type_id')
    def _check_eligible_while_draft(self):
        """Blocks a Draft record from existing at all if the employee isn't
        currently eligible -- enforced here (at the ORM's own flush/
        validation stage) in addition to the create() override, since a
        constraint is harder for anything else in the stack to bypass or
        swallow than a manually-raised exception inside create().
        """
        for rec in self:
            if rec.state != 'draft':
                continue
            rec._compute_available_el()
            if not rec.is_eligible:
                raise ValidationError(_(
                    'You are not eligible for leave encashment.\n'
                    'A minimum of %(min)s Earned Leave days is required '
                    '(you currently have %(bal)s). Please contact HR.'
                ) % {'min': MIN_ELIGIBLE_EL, 'bal': rec.available_el})

    # -------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------
    def action_submit(self):
        for rec in self:
            # Re-check the live balance at submit time, not just what was
            # cached on screen.
            rec._compute_available_el()
            if not rec.is_eligible:
                raise UserError(_(
                    'You are not eligible for leave encashment.\n'
                    'A minimum of %(min)s Earned Leave days is required '
                    '(you currently have %(bal)s). Please contact HR.'
                ) % {'min': MIN_ELIGIBLE_EL, 'bal': rec.available_el})
            if rec.days_to_encash <= 0:
                raise UserError(_('Please enter the number of leaves you want to encash.'))
            if rec.days_to_encash > rec.available_el:
                raise UserError(_(
                    'You only have %(bal)s Earned Leave days available; '
                    'you cannot encash %(req)s.'
                ) % {'bal': rec.available_el, 'req': rec.days_to_encash})
            if rec.name_seq == _('New'):
                rec.name_seq = self.env['ir.sequence'].next_by_code('hr.leave.encashment') or _('New')
            if rec.has_manager:
                rec.state = 'submitted'
                rec._notify_users(
                    rec.employee_id.parent_id.user_id,
                    summary=_('Leave Encashment approval needed'),
                    note=_('%(emp)s has requested to encash %(days)s Earned Leave days. '
                           'Please review and approve or refuse.')
                    % {'emp': rec.employee_id.name, 'days': rec.days_to_encash},
                )
            else:
                # No reporting manager on file -- skip straight to HR,
                # note why on the chatter for auditability.
                rec.state = 'manager_approved'
                rec.message_post(body=_(
                    'No Reporting Manager set on this employee record -- '
                    'manager approval step skipped automatically.'))
                rec._notify_hr_officers(
                    summary=_('Leave Encashment approval needed (no manager on file)'),
                    note=_('%(emp)s has requested to encash %(days)s Earned Leave days. '
                           'No Reporting Manager is set, so this needs HR review directly.')
                    % {'emp': rec.employee_id.name, 'days': rec.days_to_encash},
                )

    def action_manager_approve(self):
        for rec in self:
            rec._check_is_manager()
            rec.write({
                'state': 'manager_approved',
                'manager_approved_by': self.env.user.id,
                'manager_approved_date': fields.Datetime.now(),
            })
            rec._notify_hr_officers(
                summary=_('Leave Encashment approval needed'),
                note=_('%(emp)s\'s encashment request (%(days)s days) has been approved by '
                       'their manager and is now awaiting HR approval.')
                % {'emp': rec.employee_id.name, 'days': rec.days_to_encash},
            )

    def action_manager_refuse(self):
        for rec in self:
            rec._check_is_manager()
            rec.write({'state': 'refused'})
            rec._notify_users(
                rec.employee_id.user_id,
                summary=_('Leave Encashment request refused'),
                note=_('Your manager has refused your encashment request. %(reason)s')
                % {'reason': rec.refusal_reason or ''},
            )

    def action_hr_approve(self):
        self._check_hr_officer()
        for rec in self:
            if rec.state != 'manager_approved':
                raise UserError(_(
                    'This request must be approved by the Reporting Manager first.'))
            rec.write({
                'state': 'approved',
                'hr_approved_by': self.env.user.id,
                'hr_approved_date': fields.Datetime.now(),
            })
            rec._deduct_leave_balance()
            rec._send_to_payroll()
            rec._notify_users(
                rec.employee_id.user_id,
                summary=_('Leave Encashment request approved'),
                note=_('Your encashment request for %(days)s days has been approved by HR.')
                % {'days': rec.days_to_encash},
            )

    def action_hr_refuse(self):
        self._check_hr_officer()
        self.write({'state': 'refused'})
        for rec in self:
            rec._notify_users(
                rec.employee_id.user_id,
                summary=_('Leave Encashment request refused'),
                note=_('HR has refused your encashment request. %(reason)s')
                % {'reason': rec.refusal_reason or ''},
            )

    def action_reset_draft(self):
        self.write({
            'state': 'draft',
            'manager_approved_by': False,
            'manager_approved_date': False,
            'hr_approved_by': False,
            'hr_approved_date': False,
            'payroll_processed': False,
            'payslip_input_id': False,
        })

    def _check_hr_officer(self):
        if not self.env.user.has_group('hr_holidays.group_hr_holidays_user'):
            raise UserError(_('Only HR Officers can approve or refuse encashment requests.'))

    def _check_is_manager(self):
        self.ensure_one()
        if not (self.employee_id.parent_id and self.employee_id.parent_id.user_id == self.env.user) \
                and not self.env.user.has_group('hr_holidays.group_hr_holidays_user'):
            raise UserError(_("Only this employee's Reporting Manager (or HR) can approve or refuse at this stage."))

    # -------------------------------------------------------------
    # Notifications
    # -------------------------------------------------------------
    def _notify_users(self, users, summary, note):
        """Assigns a To-Do activity AND sends a direct inbox/email
        notification to each given res.users. Chatter tracking alone does
        NOT push anything to people who aren't already followers, which is
        why approvers were never being alerted before this was added.
        """
        self.ensure_one()
        users = users.filtered(lambda u: u and u.active) if users else self.env['res.users']
        if not users:
            self.message_post(body=_(
                'Could not notify an approver: no active user found to notify '
                '(e.g. the Reporting Manager\'s Employee record may not be linked '
                'to a login user). Please follow up manually.'))
            return
        for user in users:
            self.activity_schedule(
                'mail.mail_activity_data_todo',
                user_id=user.id,
                summary=summary,
                note=note,
            )
        self.message_post(
            body=note,
            partner_ids=users.mapped('partner_id').ids,
        )

    def _notify_hr_officers(self, summary, note):
        self.ensure_one()
        hr_users = self.env['res.users'].sudo().search([
            ('groups_id', '=', self.env.ref('hr_holidays.group_hr_holidays_user').id),
        ])
        self._notify_users(hr_users, summary, note)

    # -------------------------------------------------------------
    # Leave balance deduction
    # -------------------------------------------------------------
    def _deduct_leave_balance(self):
        """Records the encashed days as a validated Time Off entry (rather
        than editing any hr.leave.allocation directly). This is the
        deliberately-chosen safe approach: it works identically whether the
        employee's Earned Leave comes from Regular allocations or an
        Accrual Plan, because it only touches the "leaves taken" side of
        the balance calculation, never the "allocated" side -- so it can
        never conflict with an accrual plan's own periodic top-ups or its
        maximum-accrual ceiling logic.

        Decimal day counts (e.g. 1.75, matching a 1.75-day/month accrual)
        are fully supported: we create a small placeholder Time Off entry,
        validate it through the normal workflow so all the usual hooks run,
        then set its stored day-count directly to the exact requested value
        -- rather than trying to land a calendar date-range on an exact
        fractional number of working days, which is fragile for odd values.
        """
        self.ensure_one()
        if self.days_to_encash <= 0:
            return

        calendar = self.employee_id.resource_calendar_id or self.employee_id.company_id.resource_calendar_id
        if not calendar:
            self.message_post(body=_(
                'Encashment of %(days)s days was APPROVED, but could not be auto-deducted: '
                'no working calendar found for %(emp)s. Please deduct manually.'
            ) % {'days': self.days_to_encash, 'emp': self.employee_id.name})
            return

        date_from = fields.Datetime.now()
        try:
            # A short placeholder window (a couple of working days) purely
            # so the record creates/validates cleanly with a positive
            # day-count; the exact value gets overwritten below regardless.
            date_to = calendar.plan_days(2, date_from, compute_leaves=True)
        except Exception:
            _logger.exception('hr_leave_encashment: calendar.plan_days failed for %s', self)
            self.message_post(body=_(
                'Encashment of %(days)s days was APPROVED, but the automatic date-range '
                'calculation for deduction failed. Please deduct manually.'
            ) % {'days': self.days_to_encash})
            return

        Leave = self.env['hr.leave'].sudo()
        leave_vals = {
            'employee_id': self.employee_id.id,
            'holiday_status_id': self.leave_type_id.id,
            'leave_subtype_id': self._get_or_create_encashment_subtype().id,
            'date_from': date_from,
            'date_to': date_to,
            'name': _('%s Leave(s) - Encashment (%s)') % (self.days_to_encash, self.name_seq or ''),
        }
        leave = Leave.create(leave_vals)

        # Push the record through its normal approval life cycle rather
        # than force-writing state='validate' directly, so any related
        # hooks/constraints in hr_holidays (or your own customizations)
        # still run as they would for a normal leave request.
        try:
            if hasattr(leave, 'action_approve'):
                leave.action_approve()
            if leave.state != 'validate' and hasattr(leave, 'action_validate'):
                leave.action_validate()
        except Exception:
            _logger.exception('hr_leave_encashment: could not auto-validate deduction leave %s', leave)
            self.message_post(body=_(
                'A Time Off entry (%(name)s) was created for this encashment but could '
                'not be auto-validated -- please validate it manually so the balance '
                'updates, and check the record for any blocking constraint.'
            ) % {'name': leave.display_name})
            self.write({'deduction_leave_id': leave.id})
            return

        # Force the exact (possibly fractional) day count regardless of
        # what the calendar computed for our placeholder window, so a
        # decimal balance always deducts exactly what was encashed.
        self.env.cr.execute(
            'UPDATE hr_leave SET number_of_days = %s WHERE id = %s',
            (self.days_to_encash, leave.id),
        )
        leave.invalidate_recordset(['number_of_days'])

        self.write({'leave_deducted': True, 'deduction_leave_id': leave.id})
        self.message_post(body=_(
            'Deducted %(days)s Earned Leave day(s) from the balance via Time Off entry %(name)s.'
        ) % {'days': self.days_to_encash, 'name': leave.display_name})

    def _get_or_create_encashment_subtype(self):
        """Your Earned Leave type has 'Leave Subcategory' (hr.leave.sub.type)
        configured, which hr.leave requires whenever the chosen Leave Type
        has any subcategories linked to it. Rather than repurposing an
        existing subcategory meant for real leave reasons (Emergency,
        Sickness, etc.), we create a dedicated one so encashment deductions
        are always clearly distinguishable in reporting.
        """
        SubType = self.env['hr.leave.sub.type'].sudo()
        subtype = SubType.search([
            ('name', '=', 'Leave Encashment'),
            ('leave_type_id', 'in', [self.leave_type_id.id]),
        ], limit=1)
        if not subtype:
            subtype = SubType.create({
                'name': 'Leave Encashment',
                'leave_type_id': [(4, self.leave_type_id.id)],
                'days': 0,
                'company_id': self.company_id.id,
            })
        return subtype

    # -------------------------------------------------------------
    # Payroll hand-off
    # -------------------------------------------------------------
    def _send_to_payroll(self):
        """Called on final HR approval. Passes the RAW day-count to payroll
        via an hr.payslip.input line on the employee's current draft
        payslip, if one exists. We do not compute a monetary amount here --
        your payroll salary rule is expected to read this input's `amount`
        (the number of days) and multiply by whatever per-day rate your
        structure defines.

        If no draft payslip is found for the employee right now, we leave
        payroll_processed = False and log a chatter note so HR/Payroll can
        add the input by hand when the payslip is generated.
        """
        self.ensure_one()
        if 'hr.payslip' not in self.env:
            self.message_post(body=_(
                'Payroll module not found -- could not create a payslip input. '
                'Please process this encashment (%(days)s days) manually in payroll.'
            ) % {'days': self.days_to_encash})
            return

        payslip = self.env['hr.payslip'].sudo().search([
            ('employee_id', '=', self.employee_id.id),
            ('state', '=', 'draft'),
        ], limit=1, order='date_from desc')

        if not payslip:
            self.message_post(body=_(
                'No draft payslip found for %(emp)s yet -- this encashment '
                '(%(days)s days) was NOT automatically added to payroll. '
                'Add it as a payslip input manually once the payslip is generated.'
            ) % {'emp': self.employee_id.name, 'days': self.days_to_encash})
            return

        input_type = self._get_or_create_encashment_input_type()
        existing_input = payslip.input_line_ids.filtered(
            lambda l: l.input_type_id.id == input_type.id)
        if existing_input:
            existing_input[0].sudo().amount = self.days_to_encash
            payslip_input = existing_input[0]
        else:
            payslip_input = self.env['hr.payslip.input'].sudo().create({
                'payslip_id': payslip.id,
                'input_type_id': input_type.id,
                'amount': self.days_to_encash,
                'contract_id': payslip.contract_id.id if payslip.contract_id else False,
            })

        self.write({'payroll_processed': True, 'payslip_input_id': payslip_input.id})
        self.message_post(body=_(
            'Added %(days)s days as a "%(type)s" input on draft payslip %(slip)s.'
        ) % {'days': self.days_to_encash, 'type': input_type.name, 'slip': payslip.name or payslip.id})

    def _get_or_create_encashment_input_type(self):
        InputType = self.env['hr.payslip.input.type'].sudo()
        input_type = InputType.search([('code', '=', ENCASHMENT_INPUT_CODE)], limit=1)
        if not input_type:
            input_type = InputType.create({
                'name': 'Leave Encashment',
                'code': ENCASHMENT_INPUT_CODE,
            })
        return input_type

    # -------------------------------------------------------------
    # ORM overrides
    # -------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name_seq') or vals.get('name_seq') == _('New'):
                vals['name_seq'] = self.env['ir.sequence'].next_by_code('hr.leave.encashment') or _('New')
        records = super().create(vals_list)
        records._check_eligible_on_create()
        return records

    def _check_eligible_on_create(self):
        """Blocks the record from being saved at all -- even as a Draft --
        if the employee isn't currently eligible. Eligibility only depends
        on the live Earned Leave balance, not on days_to_encash, so this
        check makes sense the moment a record is created, before the
        employee has necessarily filled anything else in.
        """
        for rec in self:
            rec._compute_available_el()
            if not rec.is_eligible:
                raise UserError(_(
                    'You are not eligible for leave encashment.\n'
                    'A minimum of %(min)s Earned Leave days is required '
                    '(you currently have %(bal)s). Please contact HR.'
                ) % {'min': MIN_ELIGIBLE_EL, 'bal': rec.available_el})
