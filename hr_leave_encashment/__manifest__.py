{
    'name': 'HR Leave Encashment',
    'version': '17.0.1.0.0',
    'category': 'Human Resources/Time Off',
    'summary': 'Employee self-service Earned Leave encashment requests',
    'description': """
Leave Encashment
=================
Adds an "Encashment" button/menu inside Time Off where an employee can
request encashment of their Earned Leave balance.

- Name, Employee ID and Email auto-populate from the logged in employee.
- Available Earned Leave balance auto-fetched from Time Off allocations.
- Employee must have 21+ EL days available to be eligible. Below that,
  a clear "not eligible, contact HR" message is shown.
- The employee can choose to encash fewer days than their full balance,
  but never more than what is currently available.
- Simple approval workflow: Draft -> Submitted -> Approved / Refused,
  handled by HR Officers (Time Off > Administrator/Officer group).
""",
    'author': 'Ekara',
    'depends': ['hr_holidays', 'hr_payroll', 'hr_employee_extended'],
    'data': [
        'security/ir.model.access.csv',
        'security/hr_leave_encashment_security.xml',
        'data/hr_leave_encashment_sequence.xml',
        'views/hr_leave_encashment_views.xml',
        'views/hr_employee_form_views.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
