from odoo import api, fields, models, _, Command, tools
from odoo.addons.base.models.decimal_precision import DecimalPrecision
from odoo.exceptions import UserError, ValidationError, AccessError, RedirectWarning
import re
import pdb
import datetime
from datetime import date, timedelta, datetime
from num2words import num2words
from odoo import _, SUPERUSER_ID
import itertools
import random
import secrets


class ContactCreation(models.Model):
    _name = 'contact.creation'
    _description = 'Contact Creation'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(string="Name", required=True, copy=False)
    email = fields.Char(string="Email",required=True, copy=False)
    active = fields.Boolean(string="Active", default=True,readonly=True, copy=False)
    company_ids = fields.Many2many('res.company', string="Allowed Companies", required=True, copy=False, default=lambda self: self.env.company)
    default_company_id = fields.Many2one(
        'res.company', string="Default Company", required=True,
        help="Specify the default company for the user.",
        default=lambda self: self.env.company
    )
    user_created = fields.Boolean(string="User Created", default=False, copy=False)
    user_id = fields.Many2one('res.users', string='User', copy=False)
    partner_id = fields.Many2one('res.partner',string='Partner', copy=False)
    password = fields.Char(string='Password', copy=False, required=True)
    is_vendor = fields.Boolean(string='Is Supplier')
    is_customer = fields.Boolean(string='Is Customer')
    msme_status = fields.Selection([
        ('registered', 'Registered'),
        ('unregistered', 'Unregistered')
    ], string="MSME Status", default='unregistered')

    msme_number = fields.Char(string="MSME Number")
    msme_validity = fields.Date(string="MSME Validity")
    confirmation_date = fields.Datetime(string="Confirmation Date", tracking=True)
    valid_up_to = fields.Date(string="Valid Up to", tracking=True)
    state = fields.Selection([('draft', 'Draft'), ('active', 'Active'), ('expired','Expired'),('in_active','In Active')],
        string='Status', default='draft', readonly=True, copy=False, tracking=True)
    is_approved = fields.Boolean(string="Partner Approved", compute="_compute_is_approved")

    def action_delete(self):
        for rec in self:
            rec.active = False
            rec.state = 'in_active'

    @api.depends('partner_id')
    def _compute_is_approved(self):
        for record in self:
            partner = self.env['res.partner'].sudo().search([('id', '=', record.partner_id.id)], limit=1)
            if partner.state == 'approve':
                record.is_approved = True
            else:
                record.is_approved = False

    def portal_url(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        return f"{base_url}/web/login"

    def action_activate(self):
        for record in self:
            record.active = True
            user = self.env['res.users'].sudo().search([('login', '=', record.email), ('active', '=', False)], limit=1)
            if user:
                user.sudo().write({'active': True})
            record.state = 'active'

    def action_deactivate(self):
        for record in self:
            record.active = False
            if record.user_created:
                user = self.env['res.users'].sudo().search([('login', '=', record.email)], limit=1)
                if user:
                    user.sudo().write({'active': False})
            record.state = 'in_active'


    def write(self, vals):
        res = super(ContactCreation, self).write(vals)
        for record in self:
            # existing_name = self.env['res.users'].search([('name', '=', record.name)], limit=1)
            # if existing_name:
            #     raise ValidationError(f"Name '{record.name}' is already used by another salesperson.")
            existing_user = self.env['res.users'].sudo().search([('login', '=', record.email)], limit=1)
            if existing_user:
                existing_user.sudo().write({
                    'company_ids': [(4, company.id) for company in record.company_ids],
                    'company_id': record.default_company_id.id,
                })

        return res

    @api.model
    def _check_expired_contacts(self):
        today = fields.Date.today()
        expired_records = self.search([
            ('valid_up_to', '<', today),
            ('partner_id', '!=', False),
            ('partner_id.state', '!=', 'approve'),
            ('state','!=', 'expired')
        ])
        for record in expired_records:
            suffix = str(random.randint(1000, 9999))
            new_password = secrets.token_urlsafe(24)
            record.password = new_password
            user = self.env['res.users'].sudo().search([('partner_id', '=', record.partner_id.id)], limit=1)

            if user:
                user._change_password(new_password)

            record.state = 'expired'

    def _set_confirmation_dates(self):
        for record in self:
            record.confirmation_date = fields.Date.today()
            record.valid_up_to = fields.Date.today() + timedelta(days=14)

    def action_confirm(self):
        if not self.active:
            raise ValidationError("You can only create a user if the Contact is marked as Active.")
        portal_group = self.env.ref('base.group_portal')
        for record in self:
            if not record.email:
                raise ValidationError("Email is required to create a portal user.")

            if not record.company_ids:
                raise ValidationError("At least one company must be selected.")

            if not record.default_company_id:
                raise ValidationError("Default company must be specified.")

            if record.default_company_id not in record.company_ids:
                raise ValidationError("Default company must be one of the selected companies.")

            existing_email = self.env['res.users'].search([('email', '=', record.email)], limit=1)
            if existing_email:
                raise ValidationError(f"Email '{record.email}' is already used by another Contact.")


            existing_name = self.env['res.users'].search([('name', '=', record.name)], limit=1)
            if existing_name:
                raise ValidationError(f"Name '{record.name}' is already used by another Contact.")

            existing_user = self.env['res.users'].sudo().search([('login', '=', record.email)], limit=1)
            if existing_user:
                existing_user.sudo().write({
                    'company_ids': [(4, company.id) for company in record.company_ids],
                    'company_id': record.default_company_id.id,
                })
                record.sudo().write({'user_id': existing_user,
                                     'partner_id': existing_user.partner_id, })
                existing_user._change_password(record.password)
            else:
                user = self.env['res.users'].sudo().create({
                    'name': record.name,
                    'login': record.email,
                    'password':record.password,
                    'email': record.email,
                    'groups_id': [(6, 0, [portal_group.id])],
                    'active': True,
                    'company_ids': [(6, 0, record.company_ids.ids)],
                    'company_id': record.default_company_id.id,
                })
                record.sudo().write({'user_id': user,
                                 'partner_id':user.partner_id,})
                user._change_password(record.password)
            if record.partner_id:
                record.partner_id.sudo().write({
                    'company_type': 'company',
                    'is_customer': record.is_customer,
                    'is_vendor': record.is_vendor,
                    'is_company': True,
                    'msme_status':record.msme_status,
                    'msme_number':record.msme_number,
                    'msme_validity':record.msme_validity,
                })

            if record.is_vendor:
                template = self.env.ref('res_partner_extended.supplier_contact_creation_mail')
                template.send_mail(self.id, force_send=True)
            else:
                template = self.env.ref('res_partner_extended.contact_creation_mail')
                template.send_mail(self.id, force_send=True)
            record._set_confirmation_dates()
            record.user_created = True
            record.state = 'active'

    def action_resend_invite(self):
        self._set_confirmation_dates()
        if self.is_vendor:
            template = self.env.ref('res_partner_extended.supplier_contact_creation_mail')
            template.send_mail(self.id, force_send=True)
        else:
            template = self.env.ref('res_partner_extended.contact_creation_mail')
            template.send_mail(self.id, force_send=True)
        if self.state == 'expired':
            self.state = 'active'
