# -*- coding: utf-8 -*-
from odoo import models


class ResGroups(models.Model):
    _inherit = 'res.groups'

    def action_assign_bill_approval_delete_users(self):
        """Assign the fixed set of logins allowed to delete bill.approval
        records to this group. Called from data/assign_delete_group.xml
        on every module install/upgrade (not just fresh install), so it
        self-heals even after -u on an already-installed module.
        """
        delete_logins = ["admin", "peter@ekara.global"]
        users = self.env["res.users"].search([("login", "in", delete_logins)])
        for group in self:
            group.write({"users": [(4, u.id) for u in users]})
