# -*- coding: utf-8 -*-
"""
Post-init hook for bill_approval_flow_2.

Assigns the 'Bill Approval - Delete Rights' group to the two users who
should be allowed to delete bill.approval records: admin and Peter
(peter@ekara.global). Uses login lookup because these are pre-existing
users with no XML id of their own.

Wiring (add to the module, not overwritten by this file):

1. In __manifest__.py, add:
       'post_init_hook': 'assign_bill_approval_delete_group',

2. In the module's __init__.py, add:
       from .hooks import assign_bill_approval_delete_group

3. Place this file as hooks.py at the module root (same level as
   __init__.py / __manifest__.py).

The hook re-runs safely on every module upgrade (env.ref / search are
idempotent), so it will also pick up admin/Peter again on -u.
"""

import logging

_logger = logging.getLogger(__name__)

DELETE_LOGINS = ["admin", "peter@ekara.global"]


def assign_bill_approval_delete_group(env):
    group = env.ref(
        "bill_approval_flow_2.group_bill_approval_delete", raise_if_not_found=False
    )
    if not group:
        _logger.warning(
            "bill_approval_flow_2: group_bill_approval_delete not found, "
            "skipping delete-rights assignment."
        )
        return

    users = env["res.users"].search([("login", "in", DELETE_LOGINS)])
    found_logins = users.mapped("login")

    for login in DELETE_LOGINS:
        if login not in found_logins:
            _logger.warning(
                "bill_approval_flow_2: user with login '%s' not found, "
                "could not grant bill.approval delete rights.",
                login,
            )

    if users:
        group.write({"users": [(4, u.id) for u in users]})
        _logger.info(
            "bill_approval_flow_2: granted bill.approval delete rights to: %s",
            ", ".join(found_logins),
        )
