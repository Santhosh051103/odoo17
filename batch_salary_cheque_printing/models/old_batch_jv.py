
import base64
import io
import xlsxwriter
from odoo import models, fields, api,Command, _
from odoo.exceptions import ValidationError, UserError
from odoo.tools import html2plaintext
from datetime import datetime
from collections import defaultdict

class AccountBatchJV(models.Model):
    _name = "account.batch.jv"
    _description = "Batch Salary JV"
    _order = "date desc, id desc"
    _inherit = ["mail.thread", "mail.activity.mixin"]

    name = fields.Char(required=True, copy=False, string='Reference')
    date = fields.Date(required=True, copy=False, default=fields.Date.context_today, tracking=True)
    state = fields.Selection([
        ('draft', 'New'),
        ('sent', 'Sent'),
        ('reconciled', 'Reconciled'),
    ], store=True, compute='_compute_state', default='draft', tracking=True)
    journal_id = fields.Many2one(
        'account.journal',
        string='Journal',
        check_company=True,
        domain=[('type', '=', 'bank')],
        tracking=True,
    )
    cheque_format_id = fields.Many2one('cheque.format', string='Cheque Format',
                                       help='Cheque Print Formats', copy=False)
    journal_ids = fields.One2many('account.move', 'batch_journal_id', string="Journals", required=True)
    export_file_create_date = fields.Date(string='Generation Date', default=fields.Date.today, readonly=True, help="Creation date of the related export file.", copy=False)
    export_file = fields.Binary(string='File', readonly=True, help="Export file related to this batch", copy=False)
    export_filename = fields.Char(string='File Name', help="Name of the export file generated for this batch", store=True, copy=False)

    file_generation_enabled = fields.Boolean(help="Whether or not this batch payment should display the 'Generate File' button instead of 'Print' in form view.")
    cheque_number = fields.Char(string="Cheque / RTGS Slip No", copy=False)
    towards = fields.Text(string="Towards", copy=False)
    authorised_by = fields.Many2one('res.users', string="Authorised By", copy=False)
    authorised_date = fields.Date(string="Authorised Date", copy=False)
    company_currency_id = fields.Many2one(
        string="Company Currency",
        related='journal_id.company_id.currency_id',
        store=True,
    )
    currency_id = fields.Many2one('res.currency', compute='_compute_currency', store=True, readonly=True)
    amount = fields.Monetary(
        currency_field='currency_id',
        compute='_compute_from_journal_ids',
    )
    amount_total_words = fields.Char(
        string="Amount total in words",
        compute="_compute_amount_total_words",
    )
    is_lock = fields.Boolean(string='Locked')
    consolidated_jv = fields.One2many('account.move','cons_journal_id',string='Consolidated JV')
    is_consolidated = fields.Boolean(string='Is Consolidated')
    company_id = fields.Many2one('res.company',string='Company',default=lambda self:self.env.company.id)
    hr_payslip_run_id = fields.Many2one('hr.payslip.run', string='HR Payslip')
    remarks = fields.Char(string='Remarks')
    salary_payable_amount = fields.Float(string='Salary Amount',compute='compute_salary_payable_amount')
    bank_id = fields.Many2one('res.partner.bank',string='Bank Account')
    company_partner = fields.Many2one('res.partner',string='partner',related='company_id.partner_id')

    @api.depends('consolidated_jv')
    def compute_salary_payable_amount(self):
        for rec in self:
            if rec.consolidated_jv:
                # print(rec.consolidated_jv.filtered(lambda l:l.line_ids.account_id.name == 'Salary Payable').amount)
                amount = rec.consolidated_jv.line_ids.filtered(lambda l:l.account_id.name == 'Salary Payable')
                if amount:
                    rec.salary_payable_amount = amount.credit if amount.credit else amount.debit
                else:
                    rec.salary_payable_amount = 0
            else:
                rec.salary_payable_amount = 0

    def action_print_batch_salary_cheque(self):
        return self.env.ref('batch_salary_cheque_printing.print_cheque_jv_batch').report_action(self)

    def action_lock(self):
        for rec in self:
            rec.write({
                'is_lock':True
            })

    @api.depends('journal_ids')
    def _compute_from_journal_ids(self):
        for rec in self:
            if rec.journal_ids:
                rec.amount = sum(line.amount_total for line in rec.journal_ids)
            else:
                rec.amount = sum(line.amount_total for line in rec.consolidated_jv)


    @api.depends('journal_id')
    def _compute_currency(self):
        for batch in self:
            batch.currency_id = batch.journal_id.currency_id or batch.company_currency_id or self.env.company.currency_id

    @api.depends('amount', 'currency_id')
    def _compute_amount_total_words(self):
        for rec in self:
            rec.amount_total_words = rec.currency_id.amount_to_text(abs(rec.salary_payable_amount)).replace(',', '')

    @api.model_create_multi
    def create(self, vals_list):
        today = fields.Date.context_today(self)
        for vals in vals_list:
            vals['name'] = self._get_batch_name(
                vals.get('date', today),
                vals)
        return super().create(vals_list)

    @api.model
    def _get_batch_name(self,sequence_date, vals):
        if not vals.get('name'):
            sequence_code = 'account.batch.jv'
            return self.env['ir.sequence'].with_context(sequence_date=sequence_date).next_by_code(sequence_code)
        return vals['name']


    def action_print_bank_advice_jv_pdf(self):
        return self.env.ref('batch_salary_cheque_printing.action_print_batch_jv').report_action(self)

    def action_view_salary_jv(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Journal Entries',
            'view_mode': 'tree,form',
            'res_model': 'account.move',
            'domain': [('id', 'in', self.journal_ids.ids)],
        }

    def create_consolidated_jv(self):
        for rec in self:
            if rec.journal_ids:
                grouped_lines = {}

                for journal_entry in rec.journal_ids:
                    for line in journal_entry.line_ids:
                        key = (
                            line.account_id.id,
                            line.partner_id.id if line.partner_id else None,
                            line.currency_id.id if line.currency_id else None,
                            line.name,
                        )
                        if key not in grouped_lines:
                            budget_id = journal_entry.crossovered_budget.crossovered_budget_line \
                                .filtered(lambda x: x.general_budget_id.name == '70110001 Employee Salary')
                            corporate_account = self.env['account.analytic.account'].search(
                                [('name', '=', 'Corporate')], limit=1)
                            grouped_lines[key] = {
                                'account_id': line.account_id.id,
                                'name': line.name,
                                'debit': 0.0,
                                'credit': 0.0,
                                'partner_id': line.partner_id.id if line.partner_id else False,
                                'currency_id': line.currency_id.id if line.currency_id else False,
                                'amount_currency': 0.0,
                                'budget_id':budget_id.ids if line.name in ("Employee's salaries","Emplr contr. To NPS","NPS Recovery") and budget_id else False,
                                'analytic_distribution':{corporate_account.id: 100.0} if line.name in ("Employee's salaries","Emplr contr. To NPS","NPS Recovery") and corporate_account else False,
                            }

                        grouped_lines[key]['debit'] += line.debit
                        grouped_lines[key]['credit'] += line.credit
                        grouped_lines[key]['amount_currency'] += line.amount_currency

                all_lines = [(0, 0, line_vals) for line_vals in grouped_lines.values()]
                attachments = []
                move = self.env['account.move'].sudo().create({
                    'move_type': 'entry',
                    'journal_id': rec.journal_id.id,
                    'line_ids': all_lines,

                })
                for entry in rec.journal_ids:
                    if entry.state == 'posted':
                        entry.button_draft()  # Set to draft
                    entry.button_cancel()

                rec.write({
                    'consolidated_jv':move,
                    'is_consolidated':True
                })
                for entry1 in rec.journal_ids:
                    entry1.active = False
                self.action_download_salary_jv()
                for line in self:
                    line_attachments = self.env['ir.attachment'].search([
                        ('res_model', '=', line._name),
                        ('res_id', '=', line.id),
                    ])
                    for attachment in line_attachments:
                        attachments.append(
                            Command.create(attachment.copy_data({
                                'res_model': 'account.move',
                                'res_id': False,
                                'raw': attachment.raw,
                            })[0])
                        )
                move.update({
                    'attachment_ids': attachments,
                })

    def bank_advice_values(self):
        for rec in self:
            if rec.hr_payslip_run_id:
                payslips = self.env['hr.payslip'].search([
                    ('payslip_run_id', '=', self.hr_payslip_run_id.id)
                ])
                employee_payslip_dict = {}
                bank = ''
                for slip in payslips:
                    employee_name = slip.employee_id.name
                    comp = 0
                    ded = 0
                    allow = 0
                    for r in slip.line_ids.filtered(lambda x:x.salary_rule_id.category_id.name =='Basic' and x.appears_on_payslip):
                        comp += r.total
                    for r in slip.line_ids.filtered(lambda x:x.salary_rule_id.category_id.name =='Allowance' and x.appears_on_payslip):
                        allow += r.total
                    for r in slip.line_ids.filtered(lambda x:x.salary_rule_id.category_id.name =='Deduction' and x.appears_on_payslip):
                        ded += r.total
                    employee_payslip_dict[employee_name] = comp-ded+allow
                    bank = self.company_id.partner_id.bank_ids[:1]
                return {
                    'employee_payslip_dict': employee_payslip_dict,
                    'bank': bank,
                }

    def action_open_mail_wizard(self):
        """ Opens a wizard to compose an email, with relevant mail template loaded by default """
        self.ensure_one()
        # self.order_line._validate_analytic_distribution()
        lang = self.env.context.get('lang')
        mail_template = self.env.ref('batch_salary_cheque_printing.email_template_batch_jv')
        if mail_template and mail_template.lang:
            lang = mail_template._render_lang(self.ids)[self.id]

        # Generate attachments (if not already generated)
        self.action_export_jv_details_xlsx()
        # self.action_generate_pdf_attachment()

        # Search for attachments
        attachments = (self.env['ir.attachment'].search([
            ('res_model', '=', 'account.batch.jv'),
            ('res_id', '=', self.id),
            ('name', 'ilike', 'Batch_Salary_JV_Details')
        ], limit=1))

        # Prepare attachment IDs
        attachment_ids = [(4, att.id) for att in attachments]

        ctx = {
            'default_model': 'account.batch.jv',
            'default_res_ids': self.ids,
            'default_template_id': mail_template.id if mail_template else None,
            'default_composition_mode': 'comment',
            'default_attachment_ids': attachment_ids,
            'mark_so_as_sent': True,
            'default_email_layout_xmlid': 'mail.mail_notification_layout_with_responsible_signature',
            'force_email': True,
            'model_description': self.with_context(lang=lang).name,
        }
        return {
            'type': 'ir.actions.act_window',
            'view_mode': 'form',
            'res_model': 'mail.compose.message',
            'views': [(False, 'form')],
            'view_id': False,
            'target': 'new',
            'context': ctx,
        }

    def action_export_jv_details_xlsx(self):
        # Create Excel Report in Memory
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output)
        sheet = workbook.add_worksheet('Payment Details')

        # Formats
        bold = workbook.add_format({'bold': True, 'bg_color': '#D7E4BC'})
        date_format = workbook.add_format({'num_format': 'yyyy-mm-dd'})

        # Define Headers
        headers = ['Sr.No.', 'Cheque / RTGS Slip No','SENDER ACCOUNT NO', 'AMOUNT',
            'BENEFICIARY ACCOUNT NO', 'BENEFICIARY ACCOUNT NAME','BENEFICIARY IFSC',
                   'BENEFICIARY LEI (If applicable)','Remarks']

        for col, header in enumerate(headers):
            sheet.write(0, col, header, bold)
        amount_format = workbook.add_format({'num_format': '#,##0.00'})

        # Populate Data
        row = 1
        rec = self.env['hr.payslip'].sudo().search([('batch_jv_ref','=',self.name)])
        c=1
        # company_bank = self.company_id.partner_id.bank_ids[:1]
        # employee_salary = self.bank_advice_values()
        # print(employee_salary,'pppppppppppppppppppppp')
        # for slip in rec:
        #     row = row + 1
        #     sheet.write(row, 0, c or '')
        #     sheet.write(row, 1, self.cheque_number or '')
        #     sheet.write(row, 2, company_bank.acc_number or '', date_format)
        #     sheet.write(row, 3, slip.move_id.amount_total or 0.0, amount_format)
        #     sheet.write(row, 4, slip.employee_id.bank_account_id.acc_number or '')
        #     sheet.write(row, 5, slip.employee_id.bank_account_id.bank_id.name or '')
        #     sheet.write(row, 6, slip.employee_id.bank_account_id.bank_id.ifsc_code or '')
        #     sheet.write(row, 7, slip.employee_id.bank_account_id.bank_id.beneficiary_lei or '')
        #     sheet.write(row, 8, self.remarks or '')
        #     c+=1
        employee_salary = self.bank_advice_values()
        employee_dict = employee_salary.get('employee_payslip_dict', {})
        company_bank = employee_salary.get('bank')[:1]  # just in case it’s a recordset

        for slip in rec:
            row += 1
            emp_name = slip.employee_id.name
            emp_amount = employee_dict.get(emp_name, 0.0)  # get amount if name matches, else 0

            sheet.write(row, 0, c or '')
            sheet.write(row, 1, self.cheque_number or '')
            sheet.write(row, 2, company_bank.acc_number or '', date_format)
            sheet.write(row, 3, emp_amount, amount_format)  # 👈 replaced here
            sheet.write(row, 4, slip.employee_id.bank_account_id.acc_number or '')
            sheet.write(row, 5, slip.employee_id.bank_account_id.acc_holder_name or '')
            sheet.write(row, 6, slip.employee_id.bank_account_id.bank_id.ifsc_code or '')
            sheet.write(row, 7, slip.employee_id.bank_account_id.bank_id.beneficiary_lei or '')
            sheet.write(row, 8, self.remarks or '')
            c += 1

        sheet.set_column(0, 0, 5)
        sheet.set_column(1, 1, 15)
        sheet.set_column(2, 2, 10)
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
            'name': f'Batch_JV_Details_{datetime.now().strftime("%Y%m%d%H%M%S")}.xlsx',
            'type': 'binary',
            'datas': file_data,
            'store_fname': f'Batch_Payment_Details_{datetime.now().strftime("%Y%m%d%H%M%S")}.xlsx',
            'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'res_model': 'account.batch.jv',
            'res_id': self.id,
        })

        # Return the attachment download URL
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }

    def action_download_salary_jv(self):

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
        total_style1 = workbook.add_format({'num_format': '#,##0.00','align': 'right','bold': True})
        # Define Headers
        # payslip_ref = html2plaintext(self.narration)
        # payslip = self.env['hr.payslip'].sudo().search([('number', '=', str(payslip_ref))])
        month = self.date.strftime('%B')  # Full month name: "May"
        year = self.date.strftime('%Y')
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
        batch = self.hr_payslip_run_id.slip_ids

        # Dictionary to consolidate total amounts by salary rule
        consolidated_lines = {}

        for payslip in batch:
            valid_lines = payslip.line_ids.filtered(lambda l: l.salary_rule_id.appears_on_batch_report)
            for line in valid_lines:
                rule = line.salary_rule_id
                rule_id = rule.id
                if rule_id not in consolidated_lines:
                    consolidated_lines[rule_id] = {
                        'name': rule.name,
                        'code': rule.code,
                        'debit': 0.0,
                        'credit': 0.0,
                    }
                if rule.account_credit:
                    consolidated_lines[rule_id]['credit'] += line.total
                elif rule.account_debit:
                    consolidated_lines[rule_id]['debit'] += line.total
        result = list(consolidated_lines.values())
        for index, (rule_id, line) in enumerate(consolidated_lines.items(), start=1):
            sheet.write(row, 0, line.get('name') or '')
            sheet.write(row, 1, line.get('debit', '-'), amount_format)
            sheet.write(row, 2, line.get('credit', '-'), amount_format)
            row += 1
        debit = 0
        credit = 0
        for r in self.consolidated_jv.line_ids:
            if r.name == 'Adjustment Entry':
                if r.debit:
                    debit = r.debit
                    sheet.write(row, 0, r.account_id.name or '-')
                    sheet.write(row, 1,r.debit,amount_format)
                elif r.credit:
                    credit = r.credit
                    sheet.write(row, 0, r.account_id.name or '-')
                    sheet.write(row, 2,r.credit,amount_format)

        row = row+1
        total_debit = sum(line.get('debit','-') for line in consolidated_lines.values())
        total_credit = sum(line.get('credit', '-') for line in consolidated_lines.values())

        sheet.write(row, 0, 'Total', bold1)
        sheet.write(row, 1, total_debit+debit, amount_format1)
        sheet.write(row, 2, total_credit+credit, amount_format1)
        row = row+1

        # sheet.write(row, 0, 'Employee Name',bold)
        # sheet.write(row, 1, 'Employee ID',bold)
        # sheet.write(row, 2, 'Salary On Hold',bold)
        # sheet.write(row, 3, 'Parental Insurance',bold)
        # sheet.write(row, 4, 'Food Coupons', bold)
        # row = row + 1
        # salary_on_hold_total = 0
        # insurance_total = 0
        # # for rec in self.journal_ids:
        # #     row = row + 1
        # rec = self.env['hr.payslip'].sudo().search([('batch_jv_ref','=',self.name)])
        # for payslip in rec:
        #     row = row + 1
        #     parental_insurance = payslip.line_ids.filtered(lambda l: l.code == 'Other_recoveries')
        #     food_coupons = payslip.line_ids.filtered(lambda l: l.code == 'FC')
        #     salary_on_hold = payslip.line_ids.filtered(lambda l: l.code == 'SOA')
        #     sheet.write(row, 0, payslip.employee_id.name)
        #     sheet.write(row, 1, payslip.employee_id.employee_number)
        #     sheet.write(row, 2, salary_on_hold.total or 0.0, total_style)
        #     sheet.write(row, 3, parental_insurance.total or 0.0,total_style)
        #     sheet.write(row, 4, food_coupons.total or 0.0, total_style)
        #     insurance_total+=parental_insurance.total
        #     salary_on_hold_total+=salary_on_hold.total
        # rows = row+2
        # sheet.write(rows, 2, salary_on_hold_total,total_style)
        # sheet.write(rows, 3, insurance_total,total_style)
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
            'name': f'Batch_Salary_JV_{datetime.now().strftime("%d_%m_%Y")}.xlsx',
            'type': 'binary',
            'datas': file_data,
            'store_fname': f'Batch Salary_JV_{datetime.now().strftime("%Y%m%d%H%M%S")}.xlsx',
            'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'res_model': 'account.batch.jv',
            'res_id': self.id,
        })

        # Return the attachment download URL
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }


class AccountMove(models.Model):
    _inherit = "account.move"

    batch_journal_id = fields.Many2one('account.batch.jv', ondelete='set null', copy=False,
        store=True, readonly=False)
    cons_journal_id = fields.Many2one('account.batch.jv', ondelete='set null', copy=False,
        store=True, readonly=False)
    payslip_ref = fields.Char("Payslip No", compute='_compute_narration_clean', store=True)

    payslip_link = fields.Many2many('hr.payslip',string='Payslip Link')

    @api.depends('narration')
    def _compute_narration_clean(self):
        for rec in self:
            rec.payslip_ref = html2plaintext(rec.narration or '').strip()
