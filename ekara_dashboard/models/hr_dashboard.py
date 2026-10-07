from odoo import models, api
from datetime import date
from dateutil.relativedelta import relativedelta
from collections import defaultdict


class HrDashboard(models.AbstractModel):

    _name = 'hr.dashboard'
    _description = 'HR Dashboard'


    # ---------------------------------------------------------
    # COMMON DOMAIN
    # ---------------------------------------------------------

    def _employee_domain(self):

        excluded_jobs = self.env['hr.job'].search([

            ('name', 'in', [

                'Director',
                'Head - Human Resources'

            ])

        ]).ids


        return [

            ('job_id', '!=', False),

            ('job_id', 'not in', excluded_jobs),
            
            ('employee_status_payroll','!=','resigned')

        ]


    # ---------------------------------------------------------
    # MAIN DASHBOARD
    # ---------------------------------------------------------

    @api.model
    def get_dashboard_data(self):

        Employee = self.env['hr.employee']

        domain = self._employee_domain()


        # ---------------------------------------------------------
        # HEADCOUNT
        # ---------------------------------------------------------

        total = Employee.search_count(domain)

        male = Employee.search_count(
            domain + [('gender', '=', 'male')]
        )

        female = Employee.search_count(
            domain + [('gender', '=', 'female')]
        )


        # ---------------------------------------------------------
        # DIVERSITY %
        # ---------------------------------------------------------

        male_pct = 0
        female_pct = 0

        if total:

            male_pct = round((male / total) * 100, 1)
            female_pct = round((female / total) * 100, 1)


        # ---------------------------------------------------------
        # DEPARTMENT ANALYTICS
        # ---------------------------------------------------------

        departments = []

        dept_data = Employee.read_group(

            domain,

            ['department_id'],

            ['department_id']

        )

        for d in dept_data:

            if d['department_id']:

                departments.append({

                    'id': d['department_id'][0],

                    'name': d['department_id'][1],

                    'count': d['department_id_count']

                })


        # ---------------------------------------------------------
        # LOCATION ANALYTICS
        # ---------------------------------------------------------

        locations = []

        loc_data = Employee.read_group(

            domain,

            ['work_location_id'],

            ['work_location_id']

        )

        for l in loc_data:

            if l['work_location_id']:

                locations.append({

                    'id': l['work_location_id'][0],

                    'name': l['work_location_id'][1],

                    'count': l['work_location_id_count']

                })


        # ---------------------------------------------------------
        # GRADE ANALYTICS
        # ---------------------------------------------------------

        grades = []

        if 'job_level_id' in Employee._fields:

            grade_data = Employee.read_group(

                domain,

                ['job_level_id'],

                ['job_level_id']

            )

            for g in grade_data:

                if g['job_level_id']:

                    grades.append({

                        'id': g['job_level_id'][0],

                        'name': g['job_level_id'][1],

                        'count': g['job_level_id_count']

                    })

        # ---------------------------------------------------------
        # ATTRITION + RETENTION
        # ---------------------------------------------------------

        attrition_data = self._calculate_attrition()


        # ---------------------------------------------------------
        # MONTHLY TREND
        # ---------------------------------------------------------

        trends = self._monthly_attrition_trend()


        return {

            'headcount': total,

            'male': male,
            'female': female,

            'male_pct': male_pct,
            'female_pct': female_pct,

            'department': departments,

            'location': locations,

            'grade': grades,

            'attrition': attrition_data['attrition'],

            'retention': attrition_data['retention'],

            'starting_headcount':
                attrition_data['starting_headcount'],

            'ending_headcount':
                attrition_data['ending_headcount'],

            'resignations':
                attrition_data['resignations'],

            'average_headcount':
                attrition_data['average_headcount'],

            'monthly_trend': trends,

        }


    # ---------------------------------------------------------
    # ATTRITION CALCULATION
    # ---------------------------------------------------------

    def _calculate_attrition(self):

        Employee=self.env['hr.employee']

        domain=self._employee_domain()

        today=date.today()


        # FY April→March

        if today.month<4:

            fy_start=date(
                today.year-1,
                4,
                1
            )

        else:

            fy_start=date(
                today.year,
                4,
                1
            )


        # employees existing at FY start

        starting_headcount=Employee.search_count(

            domain+[

                ('create_date','<',fy_start),

                ('employee_status_payroll','!=','resigned')

            ]

        )


        # resigned during FY

        resigned_employees=Employee.search(

            domain+[

                ('employee_status_payroll','=','resigned')

            ]

        )


        resignations=0


        for emp in resigned_employees:

            resign_date=emp.write_date.date()

            if resign_date>=fy_start:

                resignations+=1


        # current active employees

        ending_headcount=Employee.search_count(

            domain+[

                ('employee_status_payroll','in',[

                    'active',
                    'onnotice'

                ])

            ]

        )


        average_headcount=(

            starting_headcount+
            ending_headcount

        )/2 if (

            starting_headcount+
            ending_headcount

        ) else 0


        attrition=0

        if average_headcount:

            attrition=round(

                (
                    resignations/
                    average_headcount
                )*100,

                1

            )


        retention=round(

            100-attrition,

            1

        )


        return{

            'starting_headcount':
                starting_headcount,

            'ending_headcount':
                ending_headcount,

            'resignations':
                resignations,

            'average_headcount':
                average_headcount,

            'attrition':
                attrition,

            'retention':
                retention

        }


    # ---------------------------------------------------------
    # MONTHLY ATTRITION TREND
    # ---------------------------------------------------------

    def _monthly_attrition_trend(self):

        Employee=self.env['hr.employee']

        domain=self._employee_domain()

        today=date.today()

        result=[]


        for i in range(5,-1,-1):

            month_start=(

                today.replace(day=1)

                -relativedelta(months=i)

            )


            month_end=(

                month_start
                +relativedelta(months=1)
                -relativedelta(days=1)

            )


            # employees existing before month

            start_hc=Employee.search_count(

                domain+[

                    ('create_date','<=',month_start),

                    ('employee_status_payroll','!=','resigned')

                ]

            )


            resignation_count=Employee.search_count(

                domain+[

                    ('employee_status_payroll','=','resigned'),

                    ('resign_date','>=',month_start),

                    ('resign_date','<=',month_end)

                ]

            )


            end_hc=max(
                start_hc-resignation_count,
                0
            )


            avg_hc=(

                start_hc+
                end_hc

            )/2 if start_hc else 0


            rate=0

            if avg_hc:

                rate=round(

                    (
                        resignation_count/
                        avg_hc
                    )*100,

                    1

                )


            result.append({

                'month':
                month_start.strftime('%b'),

                'count':rate

            })


        return result
