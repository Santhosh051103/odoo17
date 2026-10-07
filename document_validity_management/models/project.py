from odoo import models, fields, api
from datetime import timedelta,date
from odoo.exceptions import UserError

class ProjectProject(models.Model):
    _inherit = 'project.project'

    document_type_id = fields.Many2one('document.type', string="Document Type",
                                       help="Select the type of document for this project.")
    validity_start_date = fields.Date(string="Validity Start Date")
    validity_end_date = fields.Date(string="Validity End Date")
    is_document_validity_management = fields.Boolean(string="Is Document Validity Management", default=False)
    document_reminder = fields.Integer('First Reminder', help="Days before Validity End Date to send the first reminder.")
    document_reminder2 = fields.Integer('Second Reminder', help="Days before Validity End Date to start the (repeating) second reminder.")
    document_first_reminder_sent = fields.Boolean(string="First Reminder Sent", default=False, copy=False)
    closed_date = fields.Date(string='Closed Date',readonly=1)
    closed_by = fields.Many2one('res.users',string='Closed By',readonly=1)
    days_left = fields.Integer(string="Days Left", compute="_compute_days_left")
    second_person_name = fields.Many2one('res.partner', string="Second Person Name")
    company_id = fields.Many2one(
        'res.company',
        string="Company",
        default=lambda self: self.env.company,
        required=True,
    )
    status = fields.Selection(
        [
            ('active', 'Active'),
            ('expired', 'Expired'),
        ],
        string="Status",
        compute="_compute_status",
    )

    @api.depends('validity_end_date')
    def _compute_status(self):
        today = fields.Date.today()
        for record in self:
            if record.validity_end_date and record.validity_end_date < today:
                record.status = 'expired'
            else:
                record.status = 'active'

    @api.depends('validity_end_date')
    def _compute_days_left(self):
        today = date.today()
        for record in self:
            if record.validity_end_date:
                days_left = (record.validity_end_date - today).days
                record.days_left = max(days_left, 0)
            else:
                record.days_left = 0

    def document_closed(self):
        self.closed_date = date.today()
        self.closed_by = self.env.user
        self.stage_id = self.env['project.project.stage'].sudo().search([('name','=','Done')])
        self.is_done = True

    def _create_default_task_stages(self):
        context = self.env.context
        stage_names = []
        if context.get('is_legal_notice_option') or context.get('is_statuory_notice_option'):
            stage_names = ['Draft', 'In Progress', 'Review', 'Done']
        if context.get('default_is_document_validity_management'):
            stage_names = ['Draft', 'To Renew', 'Active', 'Expired']
        else:
            stage_names = ['Draft','In Progress','Done','Expired']
        if stage_names:
            for name in stage_names:
                stage = self.env['project.task.type'].search([('name', '=', name),('project_ids', 'in',self._origin.id)], limit=1)
                if not stage:
                    stage = self.env['project.task.type'].create({
                        'name': name,
                        'sequence': stage_names.index(name),
                        'fold': name in ['Expired'],
                    })
                if self.id not in stage.project_ids.ids:
                    stage.project_ids = [(4, self._origin.id)]

    @api.model_create_multi
    def create(self, vals_list):
        projects = super().create(vals_list)
        projects._create_default_task_stages()
        return projects

    @api.onchange('validity_end_date','document_reminder','document_reminder2')
    def _onchange_validity_dates(self):
        """
        Update First/Second reminder dates when validity end date or either
        reminder day-count changes. Both are plain "days before
        Validity End Date" offsets, matching the pattern used for Legal /
        Statutory Notice reminders and recurring task reminders elsewhere
        in this system.
        """
        for project in self:
            if project.is_legal_notice or project.is_document_validity_management:
                if project.validity_end_date:
                    validity_end_date = project.validity_end_date
                    if project.document_reminder:
                        project.first_reminder_date = validity_end_date - timedelta(days=project.document_reminder)
                    if project.document_reminder2:
                        project.second_reminder_date = validity_end_date - timedelta(days=project.document_reminder2)

    @api.constrains('validity_start_date', 'validity_end_date')
    def _check_date_order(self):
        for record in self:
            if record.validity_end_date < record.validity_start_date:
                raise UserError("The end date cannot be earlier than the start date.")
            if record.validity_start_date > record.validity_end_date:
                raise UserError("The start date cannot be later than the end date.")

    def send_reminder_document(self):
        today = fields.Date.today()
        # First reminder: sent once only, per document.
        first_due = self.sudo().search([
            ('is_document_validity_management', '=', True),
            ('first_reminder_date', '!=', False),
            ('first_reminder_date', '<=', today),
            ('document_first_reminder_sent', '=', False),
        ])
        # Second reminder: sent every day the cron runs, until the document
        # is marked Done - deliberate daily nag, matching the recurring
        # task reminder pattern.
        second_due = self.sudo().search([
            ('is_document_validity_management', '=', True),
            ('second_reminder_date', '!=', False),
            ('second_reminder_date', '<=', today),
            ('is_done', '=', False),
        ])

        if first_due:
            self._schedule_activities_first_reminder_document(first_due)
            self._send_first_reminder_email_notifications_document(first_due)

        if second_due:
            self._send_second_reminder_email_notifications_document(second_due)

    def _send_first_reminder_email_notifications_document(self,document_first_reminder):
        # Recipient is the project creator (create_uid), handled directly
        # in the template's own email_to field - see mail_template_data.xml.
        template = self.env.ref('document_validity_management.document_validity_first_reminder_email_template')
        for rec in document_first_reminder:
            template.send_mail(rec.id, force_send=True)
            rec.document_first_reminder_sent = True

    def _send_second_reminder_email_notifications_document(self,document_second_reminder):
        # No "sent" flag set here on purpose - this resends daily until
        # the document is marked Done.
        template = self.env.ref('document_validity_management.document_validity_second_reminder_email_template')
        for rec in document_second_reminder:
            template.send_mail(rec.id, force_send=True)

    def _schedule_activities_first_reminder_document(self, projects):
        today = fields.Date.today()
        for project in projects:
            project.activity_schedule(
                activity_type_id=self.env.ref('mail.mail_activity_data_todo').id,
                summary="Reminder: Document Validity Due",
                note="The document deadline is approaching. Please take action.",
                user_id=project.create_uid.id,
                date_deadline=today
            )

class ProjectTask(models.Model):
    _inherit = 'project.task'

    document_type_id = fields.Many2one('document.type', string="Document Type",
                                       help="Select the type of document for this project.")
    validity_start_date = fields.Date(string="Validity Start Date")
    validity_end_date = fields.Date(string="Validity End Date")
    stage_id = fields.Many2one('project.task.type', string="Stage")
    is_document_validity_management = fields.Boolean(string="Is Document Validity Management", default=False)
    days_left = fields.Integer(string="Days Left", compute="_compute_days_left")

    @api.depends('validity_end_date')
    def _compute_days_left(self):
        today = date.today()
        for record in self:
            if record.validity_end_date:
                days_left = (record.validity_end_date - today).days
                record.days_left = max(days_left, 0)
            else:
                record.days_left = 0

    @api.model
    def create(self,vals):
        project_id = self.project_id.browse(vals.get('project_id'))
        if project_id.is_document_validity_management:
            vals['is_document_validity_management'] = True
            vals['document_type_id'] = project_id.document_type_id.id or False
            if not vals.get('validity_start_date'):
                vals['validity_start_date'] = project_id.validity_start_date
            if not vals.get('validity_end_date'):
                vals['validity_end_date'] = project_id.validity_end_date
            if not vals.get('company_id'):
                vals['company_id'] = project_id.company_id.id
        res = super().create(vals)
        return res

    @api.onchange('document_type_id', 'validity_start_date')
    def _onchange_document_type(self):
        if not self.document_type_id:
            self.validity_end_date = self.validity_start_date = False
        if self.document_type_id and self.validity_start_date:
            self.validity_end_date = self.validity_start_date + timedelta(
                days=self.document_type_id.default_validity_period
            )

    @api.constrains('validity_start_date', 'validity_end_date')
    def _check_date_order(self):
        for record in self:
            if record.validity_end_date < record.validity_start_date:
                raise UserError("The end date cannot be earlier than the start date.")
            if record.validity_start_date > record.validity_end_date:
                raise UserError("The start date cannot be later than the end date.")

    def action_send_status_email(self):
        template = self.env.ref('document_validity_management.email_template_status_change')
        for task in self:
            template.send_mail(task.id, force_send=True)

    def action_send_status_email_salesperson(self):
        template = self.env.ref('document_validity_management.email_template_notify_person')
        for task in self:
            template.send_mail(task.id, force_send=True)

    @api.model
    def check_active_tasks(self):
        active_tasks = self.search([
            ('validity_start_date', '<=', fields.Date.today()),
            ('validity_end_date', '>=', fields.Date.today()),
            ('stage_id.name', '!=', 'Active'),
            ('stage_id.name', '!=', 'Expired'),
        ])
        for task in active_tasks:
            task.stage_id = self.env['project.task.type'].search([('name', '=', 'Active'),('project_ids', 'in',task.project_id.id)]).id
            task.action_send_status_email()
            task.action_send_status_email_salesperson()

    @api.model
    def check_expired_tasks(self):
        expired_tasks = self.search([
            ('validity_end_date', '<', fields.Date.today()),
            ('stage_id.name', '!=', 'Expired'),
            ('project_id.document_type_id.default_validity_period', '>',0),
        ])
        for task in expired_tasks:
            task.stage_id = self.env['project.task.type'].search([('name', '=', 'Expired'),
                                                                  ('project_ids', 'in',task.project_id.id)]).id
            if task.validity_start_date and task.validity_end_date:
                date_diff = task.validity_end_date - task.validity_start_date

                new_start_date = task.validity_end_date + timedelta(days=1)
                new_end_date = new_start_date + date_diff
            self.create({
                'name': f"{task.name}",
                'project_id': task.project_id.id,
                'document_type_id': task.document_type_id.id,
                'partner_id': task.project_id.partner_id.id if task.project_id.partner_id else False,
                'user_ids': [(6, 0, task.user_ids.ids)],
                'stage_id': self.env['project.task.type'].search([('name', '=', 'To Renew'),
                                                                  ('project_ids', 'in', task.project_id.id)]).id,
                'validity_start_date': new_start_date,
                'validity_end_date': new_end_date,
            })
            task.action_send_status_email()
            task.action_send_status_email_salesperson()
