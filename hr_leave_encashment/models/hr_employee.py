# -*- coding: utf-8 -*-
from odoo import models, _


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    def action_open_leave_encashment(self):
        """Opens a fresh Leave Encashment request pre-filled for this
        employee, as a dialog (matches the pattern of the other
        header-row actions like Send Appointment Letter)."""
        self.ensure_one()
        return {
            'name': _('Leave Encashment Request'),
            'type': 'ir.actions.act_window',
            'res_model': 'hr.leave.encashment',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_employee_id': self.id},
        }
