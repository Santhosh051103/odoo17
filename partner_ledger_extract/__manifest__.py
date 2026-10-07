{
    'name': 'Partner Ledger Extract (Customer Statement)',
    'version': '17.0.1.0.0',
    'category': 'Accounting/Reporting',
    'summary': 'Printable customer ledger extract showing both debit and credit for any partner',
    'description': """
Partner Ledger Extract
=======================
Standalone wizard + QWeb PDF report that produces a customer-invoice-style
ledger statement for one or more partners, listing every receivable/payable
account.move.line between two dates with debit, credit and a running
balance column (opening balance + closing balance included).
""",
    'author': 'Ekara Custom Development',
    'depends': ['account'],
    'data': [
        'security/ir.model.access.csv',
        'wizard/partner_ledger_wizard_view.xml',
        'report/partner_ledger_report.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
