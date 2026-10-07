{
    'name': 'Fixed Asset Register Demo',
    'version': '1.0',
    'category': 'Accounting',
    'summary': 'Simple fixed asset register with depreciation schedule',
    'depends': ['base'],
    'data': [
        'security/ir.model.access.csv',
        'data/sequence.xml',
        'views/fixed_asset_views.xml',
    ],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
