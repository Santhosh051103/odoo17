##############################################################################
#
#    Copyright Domiup (<http://domiup.com>).
#
##############################################################################


import werkzeug.urls

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import format_amount, format_date, formatLang, groupby
from datetime import datetime
import pdb

class RequestApproval(models.TransientModel):
    _name = "request.approval"
    _description = "Request Approval"

    name = fields.Char(string="Title", required=True)
    priority = fields.Selection(
        [("0", "Normal"), ("1", "Medium"), ("2", "High"), ("3", "Very High")],
        default="0",
    )
    request_date = fields.Datetime(default=fields.Datetime.now, required=True)
    type_id = fields.Many2one(
        string="Type", comodel_name="multi.approval.type", required=True
    )
    description = fields.Html()
    origin_ref = fields.Reference(string="Origin", selection="_selection_target_model")

    @api.model
    def _selection_target_model(self):
        models = self.env["ir.model"].search([])
        return [(model.model, model.name) for model in models]

    def _get_obj_url(self, obj):
        base = "web#"
        fragment = {"view_type": "form", "model": obj._name, "id": obj.id}
        url = base + werkzeug.urls.url_encode(fragment)
        return "{base}/{url}".format(
            base=self.env["ir.config_parameter"].sudo().get_param("web.base.url"),
            url=url,
        )

    @api.model
    def default_get(self, fs):
        """
        1. Get approval type
        2. Set the title as document's name
        3. Set origin
        """
        res = super().default_get(fs)
        ctx = self._context
        model_name = ctx.get("active_model")
        res_id = ctx.get("active_id")
        types = self.env["multi.approval.type"]._get_types(model_name)
        approval_type = self.env["multi.approval.type"].filter_type(
            types, model_name, res_id
        )
        if not approval_type:
            raise UserError(
                _("Data is changed! Please refresh your browser in order to continue !")
            )

        # Add the link to the source document inside the description.
        # in order to bypass the record rule on it
        record = self.env[model_name].browse(res_id)
        # Payments linked to a bill approval: stop over-limit payments here,
        # before the request form is filled in and submitted.
        if model_name == 'account.payment' and hasattr(record, '_check_limit_before_request'):
            record._check_limit_before_request()
        # Bills linked to a bill approval: stop TDS entered twice here, before
        # the request form is filled in and submitted.
        if model_name == 'account.move' and hasattr(record, '_check_limit_before_request'):
            record._check_limit_before_request()
        if model_name == 'crossovered.budget' and record.crossovered_budget_line:
            for line in record.crossovered_budget_line:
                if not line.analytic_account_id and line.user_type == 'odoo':
                    raise UserError('Kindly add a Analytic Account for a Budget Line')
                # if line.planned_amount <= 0:
                #     raise UserError('Warning !! Planned Amount Should be greater than Zero')

        record_name = record.display_name or _("this object")
        model_display_name = self.env['ir.model'].sudo().search([('model', '=', model_name)], limit=1).name or _("Unknown Model")
        title = _("Request approval for {} - {}").format(model_display_name, record_name)
        priority = '0'
        if model_name == "employee.indent":
            employee_indent = self.env['employee.indent'].browse(res_id)
            if employee_indent.priority:
                priority = employee_indent.priority
        record_url = self._get_obj_url(record)
        if approval_type.request_tmpl:
            request_tmpl = werkzeug.urls.url_unquote(_(approval_type.request_tmpl))
            descr = request_tmpl.format(
                record_url=record_url, record_name=record_name, record=record
            )
        else:
            descr = ""
        res.update(
            {
                "name": title,
                "type_id": approval_type.id,
                "origin_ref": f"{model_name},{res_id}",
                "description": descr,
                "priority": priority,
            }
        )

        #assigning the approvers uniquely
        if model_name == 'employee.indent':
            unit_head_user = record.unit_head_id.id
            recruitment_spoc_mgr_user = record.recruitment_spoc_mgr_id.id
            director_approval_user = record.director_approval_id.id

            approval_lines = self.env["multi.approval.type.line"].search([
                ('type_id', '=', approval_type.id)
            ], limit=2)
            print(approval_type.id,"Checking dafd")

            if approval_lines:
                approval_lines[0].write({
                    'user_id': [(6, 0, [unit_head_user, recruitment_spoc_mgr_user])]
                })

                approval_lines[1].write({
                    'user_id': [(6, 0, [director_approval_user])]
                })

        if model_name == 'employee.kra':
            manager_user_id = record.employee_parent_id.user_id.id
            if not manager_user_id:
                raise UserError("Manager does not have a corresponding user in the system.")

            approval_lines = self.env["multi.approval.type.line"].search([
                ('type_id', '=', approval_type.id)
            ])
            print(approval_type.id, "Checking dafd")
            if approval_lines:
                first_approval_line = approval_lines[0]
                first_approval_line.write({
                    'user_id': [(6, 0, [manager_user_id])]
                })

        if model_name == 'hr.resignation':
            manager_user = record.employee_parent_id.user_id.id
            hr_coach_user = record.coach_id.user_id.id

            approval_lines = self.env["multi.approval.type.line"].search([
                ('type_id', '=', approval_type.id)
            ], limit=2)

            if approval_lines:
                approval_lines[0].write({
                    'user_id': [(6, 0, [manager_user])]
                })

                approval_lines[1].write({
                    'user_id': [(6, 0, [hr_coach_user])]
                })

        return res

    def action_request(self):
        """
        1. create request
        2. Submit request
        3. update x_has_request_approval = True
        4. open request form view
        """
        self.ensure_one()

        if (
            not self.type_id.active
            or not self.type_id.is_configured
            or not self.origin_ref.x_need_approval
        ):
            raise UserError(
                _("Data is changed! Please refresh your browser in order to continue !")
            )
        if self.origin_ref.x_has_request_approval and not self.type_id.is_free_create:
            raise UserError(_("Request has been created before !"))
        active_res_model = self._context.get('active_model')
        if active_res_model == 'purchase.order':
            domain1 = [('id', '=', self.origin_ref.budget_id.id)]
            budget_allocated_id = self.env['crossovered.budget.lines'].sudo().search(domain1, limit=1)
            if budget_allocated_id:
                allocated_amount = budget_allocated_id.planned_amount
                spent_amount = (abs(budget_allocated_id.practical_amount) + budget_allocated_id.reserved_amount)
                available_amount = allocated_amount - spent_amount
                allocated_amount_formatted = formatLang(self.env, allocated_amount,
                                                        currency_obj=self.origin_ref.company_id.currency_id)
                available_amount_formatted = formatLang(self.env, available_amount,
                                                        currency_obj=self.origin_ref.company_id.currency_id)
                if self.origin_ref.amount_total > available_amount:
                    raise UserError(
                        _("Alert !! Budget is exceeding for %s."
                              "Allocated budget is %s and Available balance is %s.")% (self.origin_ref.budget_id.display_name, allocated_amount_formatted, available_amount_formatted)
                    )
        elif active_res_model == 'crossovered.budget':
            budget_id = self.env['crossovered.budget'].sudo().browse(self.origin_ref.id)
            if not budget_id.cash_payment_ids:
                raise UserError('Please Add Monthly breakup Lines for the Budget: %s.' %budget_id.name)

            elif not budget_id.show_budget_sum:
                raise UserError('Please Get the Cash Outflow/Inflow.')
        #checking budget Code for Accounts
        elif active_res_model == 'account.move':
            account_move_id = self.env['account.move'].sudo().browse(self.origin_ref.id)
            for move in account_move_id.filtered(lambda l: l.move_type in  ['in_invoice']):
                move.action_validate_no_bill()
            # TDS-NO-BUDGET: TDS entries have no budget and no budget impact.
            for move in account_move_id.filtered(lambda l: not l.journal_id.is_opening_balance and not l.statement_line_id
                    and not getattr(l, 'l10n_in_withholding_ref_move_id', False)):
                    # print(move.company_id.disable_budget_company,'pppppppppppppppppppp')
                    # stop
                    if move.move_type == 'entry' and not move.company_id.disable_budget_company:
                        for line1 in move.line_ids.filtered(lambda l: l.account_id.account_type in ['asset_receivable','asset_cash','asset_current','asset_non_current','asset_prepayments','asset_fixed', 'expense'] and l.account_id.is_cash_rounding == False):
                        # for line1 in move.line_ids.filtered(lambda l: l.account_id.is_cash_rounding == False):
                            if not move.crossovered_budget:
                                raise UserError('Warning!! Kindly select a Budget.')
                            if not line1.budget_id.general_budget_id.account_ids:
                                raise UserError(
                                    _("Budget Code is mandatory.\n"
                                      "To proceed without a Budget Code, please enable Disable Budget Code in the respective COA."))
                            if line1.budget_id and not line1.filtered(lambda e: e.analytic_distribution):
                                raise UserError(_("Alert !! Analytic Account not Mapped to %s for Entry -%s")%(
                                    line1.account_id.display_name,move.display_name))
                            if line1.budget_id and not line1.filtered(lambda e: {str(line1.budget_id.analytic_account_id.id): 100} == e.analytic_distribution):
                                raise UserError(_("Alert !! Wrong Analytic Account Mapped to %s.\n%s is mapped to %s Budgetry Position.")%(
                                    line1.account_id.display_name,line1.budget_id.analytic_account_id.display_name,line1.budget_id.display_name))

                    elif move.move_type != 'entry' and not move.company_id.disable_budget_company:
                        for line1 in move.invoice_line_ids.filtered(lambda l:l.account_id.is_cash_rounding == False):
                            if not move.crossovered_budget:
                                raise UserError('Warning!! Kindly select a Budget.')
                            if not line1.budget_id.general_budget_id.account_ids:
                                raise UserError(
                                    _("Budget Code is mandatory.\n"
                                      "To proceed without a Budget Code, please enable Disable Budget Code in the respective COA."))
                            if not line1.filtered(lambda e: e.analytic_distribution):
                                raise UserError(_("Alert !! Analytic Account not Mapped to %s for Entry -%s")%(
                                    line1.account_id.display_name,move.display_name))
                            if not line1.filtered(lambda e: {str(line1.budget_id.analytic_account_id.id): 100} == e.analytic_distribution):
                                raise UserError(_("Alert !! Wrong Analytic Account Mapped to %s.\n%s is mapped to %s Budgetry Position.")%(
                                    line1.account_id.display_name,line1.budget_id.analytic_account_id.display_name,line1.budget_id.display_name))
        elif active_res_model == 'project.task':
            task_id = self.env['project.task'].sudo().browse(self.origin_ref.id)
            # self.type_id.line_ids.update({'user_id':task_id.raise_request_to_id})
    
        # create request
        vals = {
            "name": self.name,
            "priority": self.priority,
            "type_id": self.type_id.id,
            "description": self.description,
            "origin_ref": f"{self.origin_ref._name},{self.origin_ref.id}",
            "company_id": self.env.company.id
        }
        request = self.env["multi.approval"].create(vals)
        request.write({'request_date': self.request_date})
        request.action_submit()
        res_model = self._context.get('active_model')
        if res_model == 'crossovered.budget':
            if not self.origin_ref.crr_share_ids:
                raise UserError(_("Can not submit for request approval without share amount."))
            self.origin_ref.approval_document = request
            self.origin_ref.state = 'to approve'
            self.origin_ref.message_post(body='Document is submitted for approval')
        if res_model == 'account.move':
            self.origin_ref.approval_document = request
            self.origin_ref.state = 'to approve'
            self.origin_ref.message_post(body='Document is submitted for approval')
        if res_model == 'purchase.order':
            self.origin_ref.approval_document = request
            self.origin_ref.state = 'to approve'
            self.origin_ref.message_post(body='Document is submitted for approval')
        if res_model == 'account.payment':
            self.origin_ref.approval_document = request
            self.origin_ref.state = 'to approve'
            self.origin_ref.message_post(body='Document is submitted for approval')
        if res_model == 'hr.expense.sheet':
            self.origin_ref.approval_document = request
            self.origin_ref.state = 'submit'
            self.origin_ref.message_post(body='Document is submitted for approval')
        if res_model == 'cash.management':
            self.origin_ref.approval_document = request
            self.origin_ref.submitted_date = datetime.now()
            self.origin_ref.submit_by = self.env.user.employee_id.id
            self.origin_ref.approval_status = 'to approve'
            self.origin_ref.message_post(body='Document is submitted for approval')
        if res_model == 'purchase.request':
            self.origin_ref.approval_document = request
            self.origin_ref.state = 'to_approve'
            self.origin_ref.message_post(body='Document is submitted for approval')
        if res_model == 'cash.requirement.report':
            # self.origin_ref.approval_document = request
            self.origin_ref.state = 'to approve'
            self.origin_ref.message_post(body='Document is submitted for approval')

        # update x_has_request_approval
        self.env["multi.approval.type"].update_x_field(
            request.origin_ref, "x_has_request_approval"
        )

        return {
            "name": _("My Requests"),
            "type": "ir.actions.act_window",
            "view_mode": "form",
            "res_model": "multi.approval",
            "res_id": request.id,
        }
