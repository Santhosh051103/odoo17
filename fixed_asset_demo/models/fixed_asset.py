from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import UserError


class FixedAsset(models.Model):
    _name = 'fixed.asset'
    _description = 'Fixed Asset'
    _order = 'purchase_date desc, id desc'

    name = fields.Char(required=True)
    code = fields.Char(readonly=True, copy=False, default='New')
    category = fields.Selection([
        ('building', 'Building'),
        ('vehicle', 'Vehicle'),
        ('furniture', 'Furniture'),
        ('it', 'IT Equipment'),
        ('machinery', 'Machinery'),
    ], required=True, default='it')
    purchase_date = fields.Date(required=True, default=fields.Date.context_today)
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company)
    currency_id = fields.Many2one(related='company_id.currency_id', store=True)
    purchase_value = fields.Monetary(required=True)
    salvage_value = fields.Monetary()
    useful_life_months = fields.Integer(default=60, required=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('running', 'Running'),
        ('disposed', 'Disposed'),
    ], default='draft', required=True)
    line_ids = fields.One2many('fixed.asset.line', 'asset_id', string='Depreciation Schedule')
    accumulated_value = fields.Monetary(compute='_compute_values', store=True)
    book_value = fields.Monetary(compute='_compute_values', store=True)

    @api.depends('purchase_value', 'line_ids.amount', 'line_ids.posted')
    def _compute_values(self):
        for rec in self:
            rec.accumulated_value = sum(rec.line_ids.filtered('posted').mapped('amount'))
            rec.book_value = rec.purchase_value - rec.accumulated_value

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('code', 'New') == 'New':
                vals['code'] = self.env['ir.sequence'].next_by_code('fixed.asset') or 'New'
        return super().create(vals_list)

    def action_confirm(self):
        for rec in self:
            if rec.useful_life_months <= 0:
                raise UserError("Useful life must be greater than zero.")
            if rec.salvage_value > rec.purchase_value:
                raise UserError("Salvage value cannot exceed purchase value.")
            rec.line_ids.unlink()
            depreciable = rec.purchase_value - rec.salvage_value
            monthly = rec.currency_id.round(depreciable / rec.useful_life_months)
            remaining = depreciable
            lines = []
            for i in range(1, rec.useful_life_months + 1):
                amount = monthly if i < rec.useful_life_months else remaining
                remaining -= amount
                lines.append((0, 0, {
                    'date': rec.purchase_date + relativedelta(months=i),
                    'amount': amount,
                }))
            rec.write({'line_ids': lines, 'state': 'running'})

    def action_post_due_lines(self):
        today = fields.Date.context_today(self)
        for rec in self:
            rec.line_ids.filtered(lambda l: not l.posted and l.date <= today).write({'posted': True})

    def action_dispose(self):
        self.write({'state': 'disposed'})

    def action_reset_draft(self):
        for rec in self:
            rec.line_ids.unlink()
            rec.state = 'draft'


class FixedAssetLine(models.Model):
    _name = 'fixed.asset.line'
    _description = 'Fixed Asset Depreciation Line'
    _order = 'date'

    asset_id = fields.Many2one('fixed.asset', required=True, ondelete='cascade')
    currency_id = fields.Many2one(related='asset_id.currency_id')
    date = fields.Date(required=True)
    amount = fields.Monetary(required=True)
    posted = fields.Boolean(default=False)
