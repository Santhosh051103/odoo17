{
    'name':'Ekara Dashboard',
    'version':'17.0.1.0',
    'author': 'Deekshith',
    'depends':['hr','web','hr_contract','hr_appraisal','survey','hr_resignation'],
    'data':[
        'security/ir.model.access.csv',
        'views/hr_dashboard_views.xml',
        'views/survey_dashboard_views.xml',
        'views/menu.xml'
    ],

    'assets': {
        'web.assets_backend': [
            'ekara_dashboard/static/src/js/hr_dashboard.js',
            'ekara_dashboard/static/src/js/survey_dashboard.js',
            'ekara_dashboard/static/src/xml/*.xml',
            'ekara_dashboard/static/src/scss/*.scss',
        ],
    },

    'installable':True,
    'application':True,
}
