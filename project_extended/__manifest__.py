{
    'name': "Project Extended",

    'description': """
        Project Extended Functionality for Ekara
        
    """,
    'category': 'Project',
    'version': '17.1',
    'depends': [
        'base',
        'mail',
        'contacts',
        'hr',
        'hr_recruitment',
        'account',
        'project',
        'hr_extended',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/project_security.xml',
        'views/project_project_views.xml',
        'views/res_company_views.xml',
        'data/mail_template_data.xml',
        'data/task_accept_email_template.xml',
        'data/project_cron.xml',
        'wizard/project_task_assign_wizard.xml',
    ],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
