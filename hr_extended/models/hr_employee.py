from odoo import models, fields, api, _
from odoo.exceptions import *
from odoo.exceptions import ValidationError, UserError


class HrLeave(models.Model):
    _inherit = "hr.leave"

    @api.model
    def get_dynamic_leave_action(self):
        """Return action with dynamic domain: self + all subordinates recursively"""
        action = self.env.ref("hr_holidays.hr_leave_action_action_approve_department").sudo().read()[0]
        user = self.env.user

        default_domain = [
            '|',
            ('employee_id.company_id', 'in', self.env.context.get('allowed_company_ids', [])),
            '&', ('multi_employee', '=', True),
            ('state', 'in', ['draft', 'confirm', 'validate1']),
            ('employee_ids.company_id', 'in', self.env.context.get('allowed_company_ids', [])),
        ]

        # Admin → see everything (keep default domain)
        if user.has_group("base.group_system"):
            action["domain"] = default_domain
        else:
            employee = user.employee_id
            if employee:
                emp_ids = self._get_all_subordinates(employee)
                emp_ids.append(employee.id)
                action["domain"] = [("employee_id", "in", emp_ids)]
            else:
                # no linked employee → show nothing
                action["domain"] = [("id", "=", 0)]

        return action

    def _get_all_subordinates(self, employee):
        """Recursively fetch all child employees and return IDs only"""
        all_children = []
        for child in employee.child_ids:
            all_children.append(child.id)
            all_children.extend(self._get_all_subordinates(child))
        return all_children


class EmployeeInsurance(models.Model):
    _name = 'employee.insurance'
    _description = 'Employee Insurance Details'

    employee_id = fields.Many2one('hr.employee', string='Employee',
                                  domain=lambda self: [('company_id', '=', self.env.company.id)], required=True,
                                  ondelete='cascade')
    insurance_holder_name = fields.Char(string='Insurance Holder Name')
    relationship = fields.Selection([
        ('self', 'Self'),
        ('spouse', 'Spouse'),
        ('child_1', 'Child 1'),
        ('child_2', 'Child 2'),
        ('father', 'Father'),
        ('mother', 'Mother'),
        ('father_in_law', 'Father-in-Law'),
        ('mother_in_law', 'Mother-in-Law'),
    ], string='Relationship', required=True)
    insurance = fields.Boolean(string='Insurance Active?')
    gmc_insurance_slab = fields.Char(string='GMC Insurance Slab')
    gpa_insurance_slab = fields.Char(string='GPA Insurance Slab')
    insured_date = fields.Date(string='Insured Date')
    annual_premium = fields.Float(string='Annual Premium')
    validity = fields.Date(string='Validity Date')


class AssetsDetails(models.Model):
    _name = 'assets.details'
    _description = 'Assets Details'

    employee_id = fields.Many2one('hr.employee', string='Employee', required=True, ondelete='cascade')
    category_id = fields.Many2one("assets.category", string="Category")
    comment = fields.Text(string='Comment')


class MailActivityPlanTemplate(models.Model):
    _inherit = 'mail.activity.plan.template'

    def _determine_responsible(self, on_demand_responsible, employee):
        if self.plan_id.res_model != 'hr.employee' or self.responsible_type not in {'coach', 'manager', 'employee'}:
            return super()._determine_responsible(on_demand_responsible, employee)

        error = False
        responsible = False

        joining_documents = self.env['joining.documents'].search([
            ('employee_id', '=', employee.id),
            ('state', '!=', 'done')
        ])
        print(joining_documents, "testingggg")

        if joining_documents:
            error = _('All joining documents for employee %s must be confirmed by respective officials', employee.name)

        if self.responsible_type == 'coach':
            if not employee.coach_id:
                error = _('Coach of employee %s is not set.', employee.name)
            responsible = employee.coach_id.user_id
            if employee.coach_id and not responsible:
                error = _("The user of %s's coach is not set.", employee.name)
        elif self.responsible_type == 'manager':
            if not employee.parent_id:
                error = _('Manager of employee %s is not set.', employee.name)
            responsible = employee.parent_id.user_id
            if employee.parent_id and not responsible:
                error = _("The manager of %s should be linked to a user.", employee.name)
        elif self.responsible_type == 'employee':
            responsible = employee.user_id
            if not responsible:
                error = _('The employee %s should be linked to a user.', employee.name)
        print(error, "trusttingggggg")
        if error or responsible:
            return {
                'responsible': responsible,
                'error': error,
            }


class HrEmployeeSmartButton(models.Model):
    _inherit = "hr.employee"

    is_self_record = fields.Boolean(
        string="Is Self Record",
        compute='_compute_is_self_record',
        store=False
    )

    @api.depends()
    def _compute_is_self_record(self):
        current_user = self.env.user
        for record in self:
            record.is_self_record = (
                    record.user_id.id == current_user.id or
                    current_user.has_group('hr.group_hr_user')
            )

    appointment_letter_sent = fields.Boolean(string="Appointment Letter Sent", default=False, copy=False)
    employee_master_insurance_ids = fields.One2many('employee.insurance', 'employee_id', string='Insurance Details')
    assets_ids = fields.One2many('assets.details', 'employee_id', string='Assets Details')

    # Family Status
    anniversary = fields.Date(string="Anniversary")
    child_dob = fields.Date(string="Child DOB")
    child_name = fields.Char(string="Child Name")

    # Present Address
    present_street = fields.Char(string="Present Street", groups="hr.group_hr_user")
    present_street2 = fields.Char(string="Present Street2", groups="hr.group_hr_user")
    present_city = fields.Char(string="Present City", groups="hr.group_hr_user")
    present_state_id = fields.Many2one(
        "res.country.state", string="Present State",
        domain="[('country_id', '=?', present_country_id)]",
        groups="hr.group_hr_user")
    present_zip = fields.Char(string="Present Zip", groups="hr.group_hr_user")
    present_country_id = fields.Many2one("res.country", string="Present Country", groups="hr.group_hr_user")
    current_leave_id = fields.Many2one('hr.leave.type', compute='_compute_current_leave',
                                       string="Current Time Off Type",
                                       groups="base.group_user")
    ongoing_appraisal_count = fields.Integer(compute='_compute_ongoing_appraisal_count', store=True,
                                             groups="base.group_user")
    message_main_attachment_id = fields.Many2one(groups="base.group_user")
    birthday = fields.Date('Date of Birth', groups="base.group_user", tracking=True)

    # Tax Related
    pf_no = fields.Char(
        string="PF Number",
        help="Provident Fund number."
    )

    uan_no = fields.Char(
        string="UAN Number",
        help="Universal Account Number (UAN) assigned to the employee."
    )
    esi_no = fields.Char(
        string="ESI Number",
        help="Employee State Insurance (ESI) number."
    )
    aadhar_no = fields.Char(
        string="Aadhar Number",
        help="Aadhar number of the employee."
    )

    # Personal details
    name_as_per_bank = fields.Char(string="Name as per Bank Details")
    father_name = fields.Char(string="Father Name")
    mother_name = fields.Char(string="Mother Name")

    job_level_id = fields.Many2one('hr.job.levels', string="Job Level")

    # last organization details
    employee_experience = fields.Float(string="Employee Experience (in years)",
                                       help="Total work experience of the employee in years."
                                       )

    last_organization = fields.Char(
        string="Last Organization",
        help="Name of the last organization where the employee worked."
    )

    last_working_day = fields.Date(
        string="Last Working Day",
        help="The last working day of the employee in their previous organization."
    )

    last_designation = fields.Char(
        string="Last Designation",
        help="The last designation the employee held in their previous organization."
    )

    city_name = fields.Char(
        string="City Name",
        help="City name where the employee is located or worked in their last organization."
    )

    last_gross_comp = fields.Float(
        string="Last Gross Compensation",
        help="The last gross compensation the employee received in their previous organization."
    )

    last_ctc = fields.Float(
        string="Last CTC",
        help="The last Cost to Company (CTC) of the employee in their previous organization."
    )

    last_variable = fields.Float(
        string="Last Variable Pay",
        help="The variable pay the employee received in their last organization."
    )

    last_esop_yes = fields.Char(
        string="Last ESOP",
        help="Whether the employee had Employee Stock Ownership Plan (ESOP) in their last organization."
    )
    last_employment_type = fields.Char(string="Last Employment Type")

    blood_group = fields.Char(string="Blood Group")

    # differenly abled
    is_differently_abled = fields.Boolean(
        string="Differently Abled",
        help="Indicates whether the employee is differently abled."
    )

    disability_type = fields.Selection(
        [
            ('physical', 'Physical Disability'),
            ('visual', 'Visual Impairment'),
            ('hearing', 'Hearing Impairment'),
            ('mental', 'Mental Disability'),
            ('other', 'Other')
        ],
        string="Type of Disability",
        help="Type of disability the employee has, if any.",
        default='other',
        track_visibility='onchange'
    )

    disability_notes = fields.Text(
        string="Disability Notes",
        help="Additional notes related to the employee’s disability."
    )

    emergency_contact_relation = fields.Char(string="Contact Relation")

    # Employee classification
    bu_head = fields.Char(string="BU Head")
    bu_unit = fields.Char(string="BU Unit")
    bu_head_id = fields.Many2one('hr.employee', string="BU Head", domain="[('company_id', '=', company_id)]", )
    business_unit_id = fields.Many2one('business.units', string="Business Unit",
                                       domain="[('company_id', '=', company_id)]", )
    sub_bu_unit = fields.Char(string="Sub BU Unit")
    separation_type = fields.Selection(
        selection=[
            ('voluntary', 'Voluntary'),
            ('involuntary', 'Involuntary'),
            ('retirement', 'Retirement'),
        ],
        string="Separation Type"
    )

    class_of_employment = fields.Char(string="Class of Employment")

    employee_status = fields.Selection(
        selection=[
            ('active', 'Active'),
            ('inactive', 'Inactive'),
            ('on_leave', 'On Leave'),
        ],
        string="Employee Status"
    )
    budgeting_units = fields.Char(string="Budgeting Units")
    budgeting_unit_id = fields.Many2one('budgeting.units', string='Budgeting Units',
                                        domain="[('company_id', '=', company_id)]",
                                        help="Select the appropriate budgeting unit.")

    employee_number = fields.Char(string="Employee Number", copy=False)
    type = fields.Selection([
        ('corporate', 'Corporate (Per Year)'),
        ('unit', 'Unit/Centre (Per Year)')], string="Unit")
    employment_type_id = fields.Many2one('hr.contract.type', string="Employment Type", copy=False)

    kra_record_ids = fields.Many2many(
        'employee.kra',
        compute='_compute_kra_records',
        string='KRA Records',
        copy=False
    )
    kra_record_count = fields.Integer(
        "KRA Record Count",
        compute='_compute_kra_records',
        default=0,
        copy=False
    )

    joining_documents_ids = fields.Many2many(
        'joining.documents',
        compute='_compute_joining_document_records',
        string='KRA Records',
        copy=False
    )
    joining_documents_count = fields.Integer(
        "Joining Documents",
        compute='_compute_joining_document_records',
        default=0,
        copy=False
    )
    project_ids = fields.Many2many(
        'project.task',
        compute='_compute_project_records',
        string='Project',
        copy=False
    )
    project_count = fields.Integer(
        "Project",
        compute='_compute_project_records',
        default=0,
        copy=False
    )
    all_documents_done = fields.Boolean(
        string='All Documents Done',
        compute='_compute_all_documents_done',
        store=True,
        readonly=True
    )

    sub_location = fields.Char(string="Sub Location", copy=False)
    sub_location_id = fields.Many2one('sub.location', string="Sub Location", domain="[('company_id', '=', company_id)]")
    last_working_day_current = fields.Date(string="Last Working Day", help="The last working day in our organization",
                                           copy=False)
    reason_for_leaving = fields.Text(string="Reason for Leaving", copy=False)
    notice_period = fields.Float(string="Notice Period", copy=False)
    notice_period_start_date = fields.Date(string="Notice Period Start Date", copy=False)
    employee = fields.Char(string="Employee", compute="_compute_employee", store=True, readonly=True, copy=False)

    casual_leave = fields.Float(string="Casual Leave", default=0)
    loss_of_pay = fields.Float(string="Loss of Pay", default=0)
    comp_off = fields.Float(string="Comp - Off", default=0)
    earned_leave = fields.Float(string="Earned Leave", default=0)
    professional_development = fields.Boolean(string="Professional Development")
    paternal_leave = fields.Float(string="Paternal Leave", default=0)
    maternal_leave = fields.Float(string="Maternal Leave", default=0)
    on_duty = fields.Float(string="On Duty", default=0)
    attachment = fields.Binary(string="Attachment")
    annual_health_check_certificate = fields.Binary(string="Annual Health Check Certificate")
    additional_documents = fields.Binary(string="Additional Documents")
    total_year_in_experience = fields.Float(string="Total Year in Experience")

    # other_fields
    new_emp_no = fields.Char(string="New Employee Number")
    sum_insured = fields.Float(string="Sum Insured")
    onboarding_id = fields.Char(string="Onboarding ID")
    year = fields.Integer(string="Year")
    appointment_order_form_q = fields.Binary(string="Appointment Order Form Q")
    mapped_pip = fields.Char(string="Mapped PIP")
    ats_id = fields.Char(string="ATS ID")
    address_details_id = fields.Many2one('res.partner', string="Address Details")
    sections = fields.Many2one('hr.department', string="Sections")
    business_processes = fields.Char(string="Business Processes")

    # base fields
    work_location_id = fields.Many2one('location.master', 'Work Location',
                                       readonly=False,
                                       domain="[('company_id', '=', company_id)]")
    employee_status_payroll = fields.Selection([
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('onnotice', 'On-Notice'),
        ('resigned', 'Resigned'),
        ('na', 'NA')], string="Employment Payroll Status")

    @api.depends('name', 'work_email')
    def _compute_employee(self):
        for record in self:
            if record.work_email and record.name:
                record.employee = f"({record.work_email}) {record.name}"
            else:
                record.employee = record.name or record.work_email or ''

    @api.depends('joining_documents_ids.state')
    def _compute_all_documents_done(self):
        for employee in self:
            joining_documents = self.env['joining.documents'].search([
                ('employee_id', '=', employee.id)
            ])
            if any(doc.state != 'done' for doc in joining_documents):
                employee.all_documents_done = False
            else:
                employee.all_documents_done = True

    # @api.model
    # def create(self, vals):
    #     if not vals.get('employee_number'):
    #         company = self.env['res.company'].browse(vals.get('company_id')) or self.env.company
    #         acronym = ''.join(word[0].upper() for word in company.name.split())
    #
    #         sequence = self.env['ir.sequence'].next_by_code('hr.employee.number') or '0001'
    #
    #         vals['employee_number'] = f"{acronym}{sequence}"
    #
    #     return super(HrEmployeeSmartButton, self).create(vals)

    # Pass applicant_id values to context of hr_contract
    def action_open_contract(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id('hr_contract.action_hr_contract')
        action['views'] = [(False, 'form')]
        if not self.contract_ids:
            action['context'] = {
                'default_employee_id': self.id,
                'default_basic_da_per_annum': self.applicant_id.basic_da_per_annum,
                'default_basic_da_per_month': self.applicant_id.basic_da_per_month,
                'default_hra_per_annum': self.applicant_id.hra_per_annum,
                'default_hra_per_month': self.applicant_id.hra_per_month,
                'default_special_allowance_per_annum': self.applicant_id.special_allowance_per_annum,
                'default_special_allowance_per_month': self.applicant_id.special_allowance_per_month,
                'default_sub_total_a_per_annum': self.applicant_id.sub_total_a_per_annum,
                'default_sub_total_a_per_month': self.applicant_id.sub_total_a_per_month,
                'default_statutory_bonus_per_annum': self.applicant_id.statutory_bonus_per_annum,
                'default_statutory_bonus_per_month': self.applicant_id.statutory_bonus_per_month,
                'default_pf_employer_per_annum': self.applicant_id.pf_employer_per_annum,
                'default_pf_employer_per_month': self.applicant_id.pf_employer_per_month,
                'default_esic_employer_per_annum': self.applicant_id.esic_employer_per_annum,
                'default_esic_employer_per_month': self.applicant_id.esic_employer_per_month,
                'default_sub_total_b_per_annum': self.applicant_id.sub_total_b_per_annum,
                'default_sub_total_b_per_month': self.applicant_id.sub_total_b_per_month,
                'default_variable_pay_per_annum': self.applicant_id.variable_pay_per_annum,
                'default_variable_pay_per_month': self.applicant_id.variable_pay_per_month,
                'default_sub_total_c_per_annum': self.applicant_id.sub_total_c_per_annum,
                'default_sub_total_c_per_month': self.applicant_id.sub_total_c_per_month,
                'default_total_salary_per_annum': self.applicant_id.total_salary_per_annum,
                'default_total_salary_per_month': self.applicant_id.total_salary_per_month,
                'default_medical_insurances': self.applicant_id.medical_insurances,
                'default_group_personal_acc_insurance': self.applicant_id.group_personal_acc_insurance,
                'default_health_ben_plan': self.applicant_id.health_ben_plan,
                'default_sub_total_d': self.applicant_id.sub_total_d,
                'default_total_ctc_annum': self.applicant_id.total_ctc_annum,
                'default_total_ctc_month': self.applicant_id.total_ctc_month,
                'default_monthly_fixed_salary': self.applicant_id.monthly_fixed_salary,
                'default_stat_bonus_amount': self.applicant_id.stat_bonus_amount,
                'default_provident_fund': self.applicant_id.provident_fund,
                'default_esi_amount': self.applicant_id.esi_amount,
                'default_variable_pay_percentage': self.applicant_id.variable_pay_percentage,
                'default_annual_store_performance_incentive': self.applicant_id.annual_store_performance_incentive,
                'default_store_performance_incentive_annum': self.applicant_id.store_performance_incentive_annum,
                'default_store_performance_incentive_month': self.applicant_id.store_performance_incentive_month,
                'default_annual_performance_linked_pay': self.applicant_id.annual_performance_linked_pay,
                'default_performance_linked_pay_annum': self.applicant_id.performance_linked_pay_annum,
                'default_performance_linked_pay_month': self.applicant_id.performance_linked_pay_month,
                'default_monthly_performance_incentive': self.applicant_id.monthly_performance_incentive,
                'default_monthly_performance_incentive_annum': self.applicant_id.monthly_performance_incentive_annum,
                'default_monthly_performance_incentive_month': self.applicant_id.monthly_performance_incentive_month,
                'default_medical_insurance': self.applicant_id.medical_insurance,
                'default_group_personal_accident_insurance': self.applicant_id.group_personal_accident_insurance,
                'default_health_benefit_plan': self.applicant_id.health_benefit_plan,
                # 'default_solis_health_benefit_beacon_plan': self.applicant_id.solis_health_benefit_beacon_plan,
                'default_indicative_take_home_salary': self.applicant_id.indicative_take_home_salary,
                'default_statutory_bonus_applicable': self.applicant_id.statutory_bonus_applicable,
                'default_provident_fund_applicable': self.applicant_id.provident_fund_applicable,
                'default_esi_applicable': self.applicant_id.esi_applicable,
                'default_location_id': self.applicant_id.locations_id.id,
                'default_grade': self.applicant_id.grade,

                # 'default_basic_da': self.applicant_id.basic_da,
                # 'default_house_rent_allowance': self.applicant_id.house_rent_allowance,
                # 'default_special_allowance': self.applicant_id.special_allowance,
                # 'default_monthly_fixed_salary': self.applicant_id.monthly_fixed_salary,
                # 'default_stat_bonus_amount': self.applicant_id.stat_bonus_amount,
                # 'default_provident_fund': self.applicant_id.provident_fund,
                # 'default_esi_amount': self.applicant_id.esi_amount,
                # 'default_variable_pay_percentage': self.applicant_id.variable_pay_percentage,
                # 'default_variable_pay_amount': self.applicant_id.variable_pay_amount,
                # 'default_annual_store_performance_incentive': self.applicant_id.annual_store_performance_incentive,
                # 'default_annual_performance_linked_pay': self.applicant_id.annual_performance_linked_pay,
                # 'default_monthly_performance_incentive': self.applicant_id.monthly_performance_incentive,
                # 'default_medical_insurance': self.applicant_id.medical_insurance,
                # 'default_group_personal_accident_insurance': self.applicant_id.group_personal_accident_insurance,
                # 'default_solis_health_benefit_beacon_plan': self.applicant_id.solis_health_benefit_beacon_plan,
                # 'default_indicative_take_home_salary': self.applicant_id.indicative_take_home_salary,
                # 'default_statutory_bonus_applicable': self.applicant_id.statutory_bonus_applicable,
                # 'default_provident_fund_applicable': self.applicant_id.provident_fund_applicable,
                # 'default_esi_applicable': self.applicant_id.esi_applicable,
            }
            action['target'] = 'new'
            return action

        target_contract = self.contract_id
        if target_contract:
            action['res_id'] = target_contract.id
            return action

        target_contract = self.contract_ids.filtered(lambda c: c.state == 'draft')
        if target_contract:
            action['res_id'] = target_contract[0].id
            return action

        action['res_id'] = self.contract_ids[0].id
        return action

    def _compute_project_records(self):
        for employee in self:
            user = employee.user_id
            if user:
                project_records = self.env['project.task'].sudo().search([('user_ids', 'in', user.id)])
                employee.project_ids = project_records
                employee.project_count = len(project_records)
            else:
                employee.project_ids = False
                employee.project_count = 0

    def _compute_joining_document_records(self):
        for employee in self:
            kra_records = self.env['joining.documents'].sudo().search([('employee_id', '=', employee.id)])
            employee.joining_documents_ids = kra_records
            employee.joining_documents_count = len(kra_records)

    def _compute_kra_records(self):
        for employee in self:
            kra_records = self.env['employee.kra'].sudo().search([('employee_id', '=', employee.id)])
            employee.kra_record_ids = kra_records
            employee.kra_record_count = len(kra_records)

    def action_open_kra_records(self):
        action = self.env.ref('hr_extended.action_view_employee_kra')
        result = action.sudo().read()[0]
        result.pop('id', None)

        kra_records = self.env['employee.kra'].sudo().search([('employee_id', '=', self.id)])
        if len(kra_records) > 1:
            result['domain'] = [('id', 'in', kra_records.ids)]
        elif len(kra_records) == 1:
            res = self.env.ref('hr_extended.view_employee_kra_form', False)
            result['views'] = [(res and res.id or False, 'form')]
            result['res_id'] = kra_records.ids[0]

        return result

    def action_get_joining_documents(self):
        self.ensure_one()
        return {
            'name': 'Joining Documents',
            'type': 'ir.actions.act_window',
            'view_mode': 'tree,form',
            'res_model': 'joining.documents',
            'domain': [('employee_id', '=', self.id)],
            'target': 'current',
        }

    def action_get_project_task(self):
        self.ensure_one()
        user = self.user_id
        if not user:
            return {
                'type': 'ir.actions.act_window_close'
            }
        return {
            'name': 'Project',
            'type': 'ir.actions.act_window',
            'view_mode': 'kanban,form',
            'res_model': 'project.task',
            'domain': [('user_ids', 'in', user.id)],
            'target': 'current',
        }

    def open_calendar_view(self):
        res = self.env['ir.actions.act_window']._for_xml_id('calendar.action_calendar_event')
        return res

    def action_open_employee_payslips(self):
        self.ensure_one()
        payslips = self.env['hr.payslip'].sudo().search([('employee_id', '=', self.id)])
        return {
            'type': 'ir.actions.act_window',
            'name': _('Payslips'),
            'res_model': 'hr.payslip',
            'view_mode': 'tree,form',
            'domain': [('id', 'in', payslips.ids)],
        }

    def action_send_appointment_letter_emp_mail(self):
        self.ensure_one()

        if not self.parent_id:
            raise UserError(_("Please set the Manager for the Employee"))

        if not self.contract_id:
            raise UserError(
                _("Employee does not have a running state Compensation Master. Please have the Running state Compensation Master"))

        if not self.joining_date:
            raise UserError(_("Please create a first Compensation Master to fill the Joining Date in 'HR Settings'"))

        if not self.private_email:
            raise UserError(_("The recipient does not have a valid email address in 'Private Information'."))

        template = self.env.ref('hr_extended.mail_appointment_letter_employee', raise_if_not_found=False)
        if not template:
            raise UserError(_("The email template for the appointment letter does not exist."))

        compose_form = self.env.ref('mail.email_compose_message_wizard_form', raise_if_not_found=True)

        self.sudo().message_follower_ids.filtered(lambda f: f.partner_id.email != self.private_email).unlink()

        ctx = dict(
            default_model='hr.employee',
            default_res_ids=self.ids,
            default_template_id=template.id,
            default_composition_mode='comment',
            default_email_layout_xmlid="mail.mail_notification_light",
            default_email_to=self.private_email,
        )

        return {
            'name': _('Send Appointment Letter'),
            'type': 'ir.actions.act_window',
            'res_model': 'mail.compose.message',
            'view_mode': 'form',
            'views': [(compose_form.id, 'form')],
            'view_id': compose_form.id,
            'target': 'new',
            'context': ctx,
        }

    # def action_send_appointment_letter_emp_mail(self):
    #     template = self.env.ref('hr_extended.mail_appointment_letter_employee')
    #     for rec in self:
    #         recipient_email = rec.private_email
    #         if not recipient_email:
    #             raise UserError(_("The recipient does not have a valid email address."))
    #
    #         template.send_mail(rec.id, force_send=True)


class PartnerBank(models.Model):
    _inherit = 'res.bank'

    ifsc_code = fields.Char(
        string="IFSC Code",
        help="The IFSC code of the bank branch."
    )
    bank_branch = fields.Char(
        string="Bank Branch",
        help="The branch of the bank."
    )
    beneficiary_lei = fields.Char(string="Beneficiary LEI",copy=False)

class HrPolicy(models.Model):
    _name = 'hr.policy'
    _description = 'HR Policy'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(string="Policy Title", required=True, tracking=True)
    description = fields.Text(string="Description")
    document = fields.Binary(string="Policy Document", attachment=True)
    filename = fields.Char(string="File Name")
    active = fields.Boolean(string="Active", default=True)
