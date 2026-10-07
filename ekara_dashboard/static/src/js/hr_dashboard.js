/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component, useState, onWillStart } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

export class EkaraDashboard extends Component {

    setup() {

        this.orm = useService("orm");
        this.action = useService("action");

        this.state = useState({

            headcount:0,
            male:0,
            female:0,

            male_pct:0,
            female_pct:0,

            attrition:0,
            retention:0,

            department:[],
            location:[],
            grade:[],

            monthly_trend:[]

        });

        onWillStart(async()=>{

            const result = await this.orm.call(
                "hr.dashboard",
                "get_dashboard_data",
                []
            );

            Object.assign(
                this.state,
                result
            );

        });

    }


    //==================================================
    // COMMON ACTION
    //==================================================

    openAction(title, domain=[]){

        const baseDomain=[

            ['job_id','!=',false],

            ['job_id.name','not in',[

                'Director',
                'Head - Human Resources'

            ]],
            ['employee_status_payroll','!=','resigned']

        ];


        this.action.doAction({

            type:"ir.actions.act_window",

            name:title,

            res_model:"hr.employee",

            views:[
                [false,"list"],
                [false,"form"]
            ],

            view_mode:"list,form",

            target:"current",

            context:{},

            domain:[
                ...baseDomain,
                ...domain
            ]

        });

    }



    //==================================================
    // KPI CARDS
    //==================================================

    openEmployees(){

        this.openAction(
            "Employees"
        );

    }


    openMaleEmployees(){

        this.openAction(

            "Male Employees",

            [

                ["gender","=","male"]

            ]

        );

    }


    openFemaleEmployees(){

        this.openAction(

            "Female Employees",

            [

                ["gender","=","female"]

            ]

        );

    }


    openAttrition(){

        this.openAction(

            "Resigned Employees",

            [

                ["employee_status_payroll","=","resigned"]

            ]

        );

    }



    //==================================================
    // DEPARTMENT
    //==================================================

    openDepartment(id){

        this.openAction(

            "Department Employees",

            [

                ["department_id","=",id]

            ]

        );

    }



    //==================================================
    // LOCATION
    //==================================================

    openLocation(id){

        this.openAction(

            "Location Employees",

            [

                ["work_location_id","=",id]

            ]

        );

    }



    //==================================================
    // JOB LEVEL / GRADE
    //==================================================

    openGrade(id){

        this.openAction(

            "Job Level Employees",

            [

                ["job_level_id","=",id]

            ]

        );

    }



    //==================================================
    // DIVERSITY
    //==================================================

    openMaleRatio(){

        this.openMaleEmployees();

    }


    openFemaleRatio(){

        this.openFemaleEmployees();

    }



    //==================================================
    // RETENTION
    //==================================================

    openRetention(){

        this.openAction(

            "Active Employees",

            [

                ["employee_status_payroll","in",[

                    "active",
                    "onnotice"

                ]]

            ]

        );

    }



    //==================================================
    // MONTHLY TREND
    //==================================================

    openMonthlyTrend(month){

        this.openAction(

            "Resigned Employees",

            [

                ["employee_status_payroll","=","resigned"]

            ]

        );

    }

}


EkaraDashboard.template =
"ekara_dashboard.Dashboard";


registry.category("actions").add(

    "ekara_dashboard_tag",

    EkaraDashboard

);