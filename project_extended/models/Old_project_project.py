# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.
from email.policy import default

from odoo import api, Command, fields, models, _, _lt
from datetime import datetime,timedelta
from odoo.exceptions import UserError, ValidationError

class AccountMove(models.Model):
    _inherit = 'account.move'

    task_id = fields.Many2one('project.task', string="Task")

    def create(self, vals_list):
        records = super().create(vals_list)

        # Ensure vals_list is always a list
        if isinstance(vals_list, dict):
            vals_list = [vals_list]

        for record, vals in zip(records, vals_list):
            task_id = vals.get('task_id')

            if task_id:
                attachments = self.env['ir.attachment'].search([
                    ('res_model', '=', 'project.task'),
                    ('res_id', '=', task_id)
                ])

                for att in attachments:
                    i = att.copy({
                        'res_model': 'account.move',
                        'res_id': record.id,
                    })
                    print(i)

        return records

class Project(models.Model):
    _inherit = "project.project"

    is_a_master = fields.Boolean(string='Is a Master')
    department_id = fields.Many2one('hr.department',string='Department')
    is_legal_notice = fields.Boolean(string="Is Legal Notice", default=False)
    is_statuory_notice = fields.Boolean(string="Is Statutory Notice", default=False)
    first_reminder_date = fields.Date(string="First Reminder Date")
    second_reminder_date = fields.Date(string="Second Reminder Date")
    date_of_notice = fields.Date(string="Date of Notice", default=fields.Date.context_today)
    last_date = fields.Date(string="Last Date")
    first_reminder = fields.Integer('First Reminder',readonly=0)
    second_reminder = fields.Integer('Second Reminder',readonly=0)
    is_done = fields.Boolean(string='Done')
    has_task_stage_changed = fields.Boolean(string='Has Task Stage Changed', copy=False)
    document_count = fields.Integer(string="Documents", compute="_compute_document_count")

    # @api.depends('date_of_notice', 'last_date')
    # def _compute_reminder_dates(self):
    #     for project in self:
    #         if project.date_of_notice and project.last_date:
    #             notice_date = fields.Date.from_string(project.date_of_notice)
    #             last_date = fields.Date.from_string(project.last_date)
    #
    #             # Calculate total duration between `date_of_notice` and `last_date`
    #             total_days = (last_date - notice_date).days
    #
    #             # Calculate First Reminder:
    #             first_reminder_days = total_days // 3
    #             first_reminder_date = notice_date + timedelta(days=first_reminder_days)
    #             project.first_reminder_date = first_reminder_date
    #             project.first_reminder = first_reminder_days
    #
    #             # Calculate Second Reminder: 2 days before `last_date`
    #             second_reminder_date = last_date - timedelta(days=2)
    #             project.second_reminder_date = second_reminder_date
    #
    #             # Calculate days between `date_of_notice` and `second_reminder_date`
    #             second_reminder_days = (second_reminder_date - notice_date).days
    #             project.second_reminder = second_reminder_days
    #         else:
    #             project.first_reminder_date = False
    #             project.first_reminder = 0
    #             project.second_reminder_date = False
    #             project.second_reminder = 0

    def _compute_document_count(self):
        Task = self.env['project.task']
        for record in self:
            tasks = Task.search([('project_id', '=', record.id)])
            subtasks = Task.search([('parent_id', 'in', tasks.ids)])
            related_ids = [(record._name, record.id)] + [('project.task', tid) for tid in tasks.ids + subtasks.ids]

            domain = []
            for model, res_id in related_ids:
                domain.append('|')
            domain = domain[:-1]
            for model, res_id in related_ids:
                domain.extend(['&', ('res_model', '=', model), ('res_id', '=', res_id)])

            attachments = self.env['ir.attachment'].search(domain)
            docs = self.env['documents.document'].search([('attachment_id', 'in', attachments.ids)])
            record.document_count = len(docs)

    def action_open_documents(self):
        self.ensure_one()

        folder = self.documents_folder_id

        Task = self.env['project.task']
        all_tasks = Task.search([('project_id', '=', self.id)])
        sub_tasks = Task.search([('parent_id', 'in', all_tasks.ids)])
        all_task_ids = all_tasks.ids + sub_tasks.ids

        attachments = self.env['ir.attachment'].search([
            '|','&', ('res_model', '=', self._name), ('res_id', '=', self.id),
            '&', ('res_model', '=', 'project.task'), ('res_id', 'in', all_task_ids),
        ])

        docs = self.env['documents.document'].search([('attachment_id', 'in', attachments.ids)])

        return {
            'type': 'ir.actions.act_window',
            'name': _('Documents'),
            'res_model': 'documents.document',
            'view_mode': 'kanban',
            'domain': [('id', 'in', docs.ids)],
            'context': {
                'searchpanel_default_folder_id': folder.id if folder else False,
                'search_default_folder_id': folder.id if folder else False,
                'default_folder_id': folder.id if folder else False,
            },
            'view_id': self.env.ref('documents.document_view_kanban').id,
        }

    @api.onchange('last_date','first_reminder','second_reminder')
    def _onchange_dates(self):
        """
        Update reminder fields when the date_of_notice or last_date changes.
        """
        for project in self:
            if project.is_legal_notice or project.is_statuory_notice:
                if project.last_date:
                    date_of_notice = project.date_of_notice
                    last_date = project.last_date
                    if last_date < fields.Date.today():
                        raise UserError(_("Kindly provide the correct date."))
                    else:
                        if project.first_reminder:
                            first_reminder = project.first_reminder
                            project.first_reminder_date = last_date - timedelta(days=first_reminder)
                        if project.second_reminder:
                            second_reminder = project.second_reminder
                            project.second_reminder_date = last_date - timedelta(days=second_reminder)

    def send_reminder(self):
        today = fields.Date.today()
        projects = self.sudo().search([
            ('first_reminder_date', '<=', today),
            ('last_date', '>=', today),
            '|', ('is_legal_notice', '=', True), ('is_statuory_notice', '=', True),
        ])

        stagnant_projects = self.env['project.project']
        for project in projects:
            tasks = self.env['project.task'].search([
                ('project_id', '=', project.id),
                ('date_last_stage_update', '!=', False),
            ])
            unchanged_tasks = tasks.filtered(
                lambda t: t.date_last_stage_update.replace(microsecond=0) == t.create_date.replace(microsecond=0)
            )
            if unchanged_tasks:
                reminder_cutoff = project.first_reminder_date - timedelta(days=1)
                # recent_update = max(tasks.mapped('date_last_stage_update')).date()
                stale_tasks = tasks.filtered(lambda t: t.date_last_stage_update.date() <= reminder_cutoff)
                if project.first_reminder_date and stale_tasks:
                    stagnant_projects |= project
            elif not tasks:
                stagnant_projects |= project

        legal_reminders = stagnant_projects.filtered(lambda p: p.is_legal_notice)
        statuory_reminders = stagnant_projects.filtered(lambda p: p.is_statuory_notice)

        if legal_reminders:
            self._schedule_activities_first_reminder_legal(legal_reminders)
            self._send_first_reminder_email_notifications_legal(legal_reminders)

        if statuory_reminders:
            self._schedule_activities_first_reminder_statuory(statuory_reminders)
            self._send_first_reminder_email_notifications_statuory(statuory_reminders)

    # def send_reminder(self):
    #     today = fields.Date.today()
    #     legal_first_reminder = self.sudo().search([
    #         ('first_reminder_date', '=', today),('is_legal_notice','=',True)
    #     ])
    #     legal_second_reminder = self.sudo().search([
    #         ('second_reminder_date', '=', today),('is_legal_notice','=',True)
    #     ])
    #     statuory_first_reminder = self.sudo().search([
    #         ('first_reminder_date', '=', today),('is_statuory_notice','=',True)
    #     ])
    #     statuory_second_reminder = self.sudo().search([
    #         ('second_reminder_date', '=', today),('is_statuory_notice','=',True)
    #     ])
    #     if legal_first_reminder:
    #         self._schedule_activities_first_reminder_legal()
    #         self._send_first_reminder_email_notifications_legal(legal_first_reminder)
    #     if legal_second_reminder:
    #         self._schedule_activities_second_reminder_legal()
    #         self._send_second_reminder_email_notifications_legal(legal_second_reminder)
    #     if statuory_first_reminder:
    #         self._schedule_activities_first_reminder_statuory()
    #         self._send_first_reminder_email_notifications_statuory(statuory_first_reminder)
    #     if statuory_second_reminder:
    #         self._schedule_activities_second_reminder_statuory()
    #         self._send_second_reminder_email_notifications_statuory(statuory_second_reminder)

    @api.model
    def create(self,vals):
            account_manager_group = self.env.ref('account.group_account_manager')
            emails = [user.email for user in account_manager_group.users if user.email]
            if emails:
                if vals.get('is_legal_notice'):
                    template = self.env.ref('project_extended.legal_notice_creation_email_template')
                    template.write({'email_to': ', '.join(emails),
                                    'subject':'Legal Notice Project Creation - %s'%(vals.get('name'))})
                    template.send_mail(self.id, force_send=True)
                elif vals.get('is_statuory_notice'):
                    template = self.env.ref('project_extended.statuory_notice_creation_email_template')
                    template.write({'email_to': ', '.join(emails),
                                    'subject':'Statutory Notice Project Creation - %s'%(vals.get('name'))})
                    template.send_mail(self.id, force_send=True)
            vals['company_id'] = self.env.company.id
            res = super().create(vals)
            return res

    @api.model
    def read(self, fields=None, load='_classic_read'):
        result = super(Project, self).read(fields, load)
        for record in result:
            record['is_legal_notice'] = self.env.context.get('is_legal_notice_option', False)
            record['is_statuory_notice'] = self.env.context.get('is_statuory_notice_option', False)
        return result

    def _schedule_activities_first_reminder_legal(self, legal_reminders):
        today = fields.Date.today()
        # projects = self.search([
        #     ('first_reminder_date', '=', today),('is_legal_notice','=',True)
        # ])
        for project in legal_reminders:
            project.activity_schedule(
                activity_type_id=self.env.ref('mail.mail_activity_data_todo').id,
                summary="First Reminder: Legal Notice Due",
                note="The legal notice deadline is approaching. Please take action.",
                user_id=project.user_id.id,
                date_deadline=fields.Date.today()
            )

    def _schedule_activities_first_reminder_statuory(self, statuory_reminders):
        today = fields.Date.today()
        # projects = self.search([
        #     ('first_reminder_date', '=', today),('is_statuory_notice','=',True)
        # ])
        for project in statuory_reminders:
            project.activity_schedule(
                activity_type_id=self.env.ref('mail.mail_activity_data_todo').id,
                summary="First Reminder: Statutory Notice Due",
                note="The Statutory notice deadline is approaching. Please take action.",
                user_id=project.user_id.id,
                date_deadline=fields.Date.today()
            )

    def _schedule_activities_second_reminder_legal(self, legal_second_reminder):
        today = fields.Date.today()
        # projects = self.search([
        #     ('second_reminder_date', '=', today),('is_legal_notice','=',True)
        # ])
        for project in legal_second_reminder:
            project.activity_schedule(
                activity_type_id=self.env.ref('mail.mail_activity_data_todo').id,
                summary="Second Reminder: Legal Notice Due",
                note="The legal notice deadline is approaching. Please take action.",
                user_id=project.user_id.id,
                date_deadline=fields.Date.today()
            )

    def _schedule_activities_second_reminder_statuory(self, statuory_second_reminder):
        today = fields.Date.today()
        # projects = self.search([
        #     ('second_reminder_date', '=', today),('is_statuory_notice','=',True)
        # ])
        for project in statuory_second_reminder:
            project.activity_schedule(
                activity_type_id=self.env.ref('mail.mail_activity_data_todo').id,
                summary="Second Reminder: Statutory Notice Due",
                note="The Statutory notice deadline is approaching. Please take action.",
                user_id=project.user_id.id,
                date_deadline=fields.Date.today()
            )

    def _send_first_reminder_email_notifications_legal(self,legal_first_reminder):
        account_manager_group = self.env.ref('account.group_account_manager')
        emails = [user.email for user in account_manager_group.users if user.email]
        if emails:
            for rec in legal_first_reminder:
                template = self.env.ref('project_extended.legal_notice_first_reminder_email_template')
                template.write({'email_to': ', '.join(emails)})
                self.env['mail.template'].browse(template.id).send_mail(rec.id, force_send=True)

    def _send_second_reminder_email_notifications_legal(self,legal_second_reminder):
        account_manager_group = self.env.ref('account.group_account_manager')
        emails = [user.email for user in account_manager_group.users if user.email]
        if emails:
            for rec in legal_second_reminder:
                template = self.env.ref('project_extended.legal_notice_second_reminder_email_template')
                template.write({'email_to': ', '.join(emails)})
                self.env['mail.template'].browse(template.id).send_mail(rec.id, force_send=True)

    def _send_first_reminder_email_notifications_statuory(self,statuory_first_reminder):
        account_manager_group = self.env.ref('account.group_account_manager')
        emails = [user.email for user in account_manager_group.users if user.email]
        if emails:
            for rec in statuory_first_reminder:
                template = self.env.ref('project_extended.statuory_notice_first_reminder_email_template')
                template.write({'email_to': ', '.join(emails)})
                self.env['mail.template'].browse(template.id).send_mail(rec.id, force_send=True)

    def _send_second_reminder_email_notifications_statuory(self,statuory_second_reminder):
        account_manager_group = self.env.ref('account.group_account_manager')
        emails = [user.email for user in account_manager_group.users if user.email]
        if emails:
            for rec in statuory_second_reminder:
                template = self.env.ref('project_extended.statuory_notice_second_reminder_email_template')
                template.write({'email_to': ', '.join(emails)})
                self.env['mail.template'].browse(template.id).send_mail(rec.id, force_send=True)


CLOSED_STATES = {
    '1_done': 'Done',
    '1_canceled': 'Canceled',
}


class ProjectTaskType(models.Model):
    _inherit = 'project.task.type'

    is_done_stage = fields.Boolean(string='Is Done Stage?')


class ProjectTask(models.Model):
    _inherit = 'project.task'

    recurrence_reminder  =fields.Integer(string='First Reminder')
    recurrence_reminder2 = fields.Integer(string='Secondary Reminder')
    recurring_start_date = fields.Date(string="Start Date")
    first_reminder_date = fields.Date(string="First Reminder Date")
    second_reminder_date = fields.Date(string="Second Reminder Date")
    task_valid_from = fields.Date(string="Task Period From",default=fields.Date.context_today)
    task_done = fields.Boolean(string='Task Done')
    task_approved = fields.Boolean(string='Task Approved')
    task_rejected = fields.Boolean(string='Task Rejected')
    task_assign_line_ids = fields.One2many('task.assign.line', 'task_id', string='Task Assign Details', copy=False)
    is_done_stage = fields.Boolean(string='Is Done Stage?', related='stage_id.is_done_stage')
    task_accepted = fields.Boolean(string='Task Accepted',default=False)
    raise_request_to_id = fields.Many2one('res.users',string='Raise Request To')
    allow_bill_creation = fields.Boolean(string="Allow Bill Creation")
    hide_bill_creation = fields.Boolean(string="Hide Bill Creation")

    def action_view_bills(self):
        self.ensure_one()

        return {
            'type': 'ir.actions.act_window',
            'name': 'Bills & Journals',
            'res_model': 'account.move',
            'view_mode': 'tree,form',
            'domain': [('task_id', '=', self.id)],
            'context': {
                'default_task_id': self.id,
                'default_move_type': 'in_invoice',
            },
        }

    def action_open_bill(self):
        self.ensure_one()
        # self.hide_bill_creation = True
        return {
            'type': 'ir.actions.act_window',
            'name': 'Bills',
            'res_model': 'account.move',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_move_type': 'in_invoice',
                'default_task_id': self.id,  # ✅ only this
            }
        }

    def action_open_journal(self):
        self.ensure_one()
        # self.hide_bill_creation = True
        return {
            'type': 'ir.actions.act_window',
            'name': 'Journals',
            'res_model': 'account.move',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_move_type': 'entry',
                'default_task_id': self.id,  # ✅ only this
            }
        }

    def action_accept(self):
        self.task_accepted = True
        line_ids = self.task_assign_line_ids.filtered(lambda x:x.assign_state=='draft')
        line_ids.update({'assign_state':'in_progress'})
        self.stage_id = self.env['project.task.type'].search([('name', '=', 'In Progress'),
                                                              ('project_ids', 'in', self.project_id.id)]).id


    def action_refuse(self):
        self.task_accepted = True
        line_ids = self.task_assign_line_ids.filtered(lambda x: x.assign_state == 'draft')
        line_ids.update({'assign_state': 'refuse'})

    def action_assign_task_user(self):
        if self.stage_id and self.stage_id.is_done_stage:
            raise ValidationError(_("Cannot assign this task as it is already moved to %s stage") % self.stage_id.name)
        return {
            'name': "Assign To",
            'type': 'ir.actions.act_window',
            'res_model': 'project.task.assign.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_task_id': self.id,
            }
        }

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        for task in res:
            if task.user_ids:
                task_assign_vals = []
                for user in task.user_ids:
                    assign_vals = {
                        'assigned_by_user_id': self.env.user.id,
                        'assigned_user_id': user.id,
                        'assigned_reason': '',
                        'assigned_date': fields.Datetime.now(),
                    }
                    task_assign_vals.append((0, 0, assign_vals))
                task.write({'task_assign_line_ids': task_assign_vals})
            if not task.raise_request_to_id:
                task.raise_request_to_id = self.env.user.id
        return res


    @api.onchange('stage_id')
    def onchange_stage_id(self):
        # if self.stage_id.name == 'Done':
        #     self.task_done = True
        if self.is_done_stage:
            self.task_done = True
            if 'x_need_approval' in self._fields:
                self.x_need_approval = True
                assigning_user_line = self._origin.task_assign_line_ids.filtered(lambda x: x.assigned_user_id.id == self.env.user.id)
                if assigning_user_line:
                    assigning_user_line[0].write({'completed_date': fields.Datetime.now(), 'assign_state': 'done'})
        else:
            self.task_done = False
            if 'x_need_approval' in self._fields:
                self.x_need_approval = False
        # else:
        #     self.task_done = True

    def action_task_approve(self):
        self.task_approved = True
        self.stage_id = self.env['project.task.type'].search([('name', '=', 'Done'),
                                                              ('project_ids', 'in', self.project_id.id)]).id
        line_ids = self.task_assign_line_ids.filtered(lambda x: x.assign_state == 'in_progress')
        line_ids.update({'assign_state': 'done'})

    def action_task_reject(self):
        self.task_rejected = True
    @api.model
    def check_expired_tasks_recurring(self):
        expired_tasks = self.search([
            ('date_deadline', '<', fields.Date.today()),
            ('stage_id.name', '!=', 'Expired'),
            ('recurring_task','=',True),
        ])
        for task in expired_tasks:
            task.stage_id = self.env['project.task.type'].search([('name', '=', 'Expired'),
                                                                  ('project_ids', 'in', task.project_id.id)]).id
            # task.action_send_status_email()

    # def action_send_status_email(self):
    #     template = self.env.ref('project_extended.email_template_status_change_task')
    #     for task in self:
    #         template.send_mail(task.id, force_send=True)

    @api.onchange('recurrence_reminder','recurrence_reminder2')
    def _onchange_dates(self):
        """
        Update reminder fields when the date_of_notice or last_date changes.
        """
        for task in self:
            if task.recurring_task:

                if task.date_deadline:
                    end_date =  task.date_deadline
                    date_str = task.date_deadline.strftime("%Y-%m-%d")
                    date = datetime.strptime(date_str, "%Y-%m-%d").date()
                    if date < fields.Date.today():
                        raise UserError(_("Kindly provide the correct date."))
                    else:
                        if task.recurrence_reminder:
                            first_reminder = task.recurrence_reminder
                            task.first_reminder_date = date - timedelta(days=first_reminder)
                        if task.recurrence_reminder2:
                            second_reminder = task.recurrence_reminder2
                            task.second_reminder_date = date - timedelta(days=second_reminder)

    def send_recurring_reminder(self):
        today = fields.Date.today()
        recurring_first_reminder = self.sudo().search([
            ('first_reminder_date', '=', today),('recurring_task','=',True)
        ])
        recurring_second_reminder = self.sudo().search([
            ('second_reminder_date', '=', today),('recurring_task','=',True)
        ])
        if recurring_first_reminder:
            self._send_first_reminder_email_notifications_recurring(recurring_first_reminder)
        if recurring_second_reminder:
            self._send_second_reminder_email_notifications_recurring(recurring_second_reminder)

    def _send_second_reminder_email_notifications_recurring(self,recurring_second_reminder):
        account_manager_group = self.env.ref('account.group_account_manager')
        emails = [user.email for user in account_manager_group.users if user.email]
        if emails:
            for rec in recurring_second_reminder:
                template = self.env.ref('project_extended.recurring_second_reminder_email_template')
                template.write({'email_to': ', '.join(emails)})
                self.env['mail.template'].browse(template.id).send_mail(rec.id, force_send=True)

    def _send_first_reminder_email_notifications_recurring(self,recurring_first_reminder):
        account_manager_group = self.env.ref('account.group_account_manager')
        emails = [user.email for user in account_manager_group.users if user.email]
        if emails:
            for rec in recurring_first_reminder:
                template = self.env.ref('project_extended.recurring_first_reminder_email_template')
                template.write({'email_to': ', '.join(emails)})
                self.env['mail.template'].browse(template.id).send_mail(rec.id, force_send=True)


    def _cron_inverse_state(self):
        today = fields.Date.today()
        start_of_day = today.strftime('%Y-%m-%d 00:00:00')
        end_of_day = today.strftime('%Y-%m-%d 23:59:59')

        task_obj = self.env['project.task'].search([
            ('planned_date_begin', '>=', start_of_day),
            ('planned_date_begin', '<=', end_of_day)
        ])
        print(task_obj,'lllll')
        for task in task_obj:
            last_task_id_per_recurrence_id = task.recurrence_id._get_last_task_id_per_recurrence_id()
            if task.state in CLOSED_STATES and task.id == last_task_id_per_recurrence_id.get(task.recurrence_id.id):
                task.recurrence_id._create_next_occurrence(task)
class ProjectTaskRecurrence(models.Model):
    _inherit = 'project.task.recurrence'
    # def _create_next_occurrence(self, occurrence_from):
    #     self.ensure_one()
    #     if self.repeat_type == 'until' and fields.Date.today() > self.repeat_until:
    #         return
    #     # Prevent double mail_followers creation
    #     self = self.with_context(mail_create_nosubscribe=True)
    #     print("fucntion triggered")
    #     # Check if the date field in the task matches today's date
    #     if occurrence_from.recurring_start_date and occurrence_from.recurring_start_date == fields.Date.today():
    #         print(occurrence_from.recurring_start_date,"recuring date")
    #         self.env['project.task'].sudo().create(
    #             self._create_next_occurrence_values(occurrence_from)
    #         )


    def _create_next_occurrence(self, occurrence_from):
        self.ensure_one()
        self = self.with_context(mail_create_nosubscribe=True)
        create_values = self._create_next_occurrence_values(occurrence_from)
        date_deadline = create_values['date_deadline']
        planned_date_begin = create_values['planned_date_begin']
        date_str = occurrence_from.planned_date_begin.strftime("%Y-%m-%d")
        date = datetime.strptime(date_str, "%Y-%m-%d").date()
        if not (self.repeat_type == 'until' and date_deadline and date_deadline.date() > self.repeat_until):
            if date == fields.Date.today():
                task = self.env['project.task'].sudo().create(create_values)
                task.write({
                    'recurrence_reminder':occurrence_from.recurrence_reminder,
                    'recurrence_reminder2':occurrence_from.recurrence_reminder2
                })


class TaskAssignLine(models.Model):
    _name = 'task.assign.line'

    task_id = fields.Many2one('project.task', string='Task', ondelete='cascade')
    assigned_by_user_id = fields.Many2one('res.users', string='Assigned By')
    assigned_user_id = fields.Many2one('res.users', string='Assigned User')
    assigned_reason = fields.Text(string='Reason')
    assigned_date = fields.Datetime(string='Assigned Date')
    completed_date = fields.Datetime(string='Completed Date')
    assign_state = fields.Selection([('draft', 'Draft'),('in_progress', 'In Progress'),('refuse', 'Refuse'), ('done', 'Done')], string='Status', default='draft')
