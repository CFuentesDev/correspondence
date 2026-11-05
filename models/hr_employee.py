# -*- coding: utf-8 -*-

from odoo import models, fields, api

class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    director_description = fields.Html(string="Descripción del Cargo")
    director_signature = fields.Binary(string="Firma del Director", attachment=True)
    director_stamp = fields.Binary(string="Sello del Director", attachment=True)

    correspondence_director_history_ids = fields.One2many(
        'correspondence.department.director',
        'director_id',
        string="Historial de Director de Correspondencia"
    )

    is_correspondence_manager = fields.Boolean(
        string="Es Gerente de Correspondencia",
        compute='_compute_is_correspondence_manager',
        store=False, # No es necesario almacenar, se calcula al vuelo
        search='_search_is_correspondence_manager'
    )

    @api.depends('correspondence_director_history_ids.date_start', 'correspondence_director_history_ids.date_end')
    def _compute_is_correspondence_manager(self):
        today = fields.Date.today()
        managed_dept_manager_ids = set(
            self.env['hr.department'].sudo().search([('manager_id', 'in', self.ids)]).mapped('manager_id').ids
        )
        for employee in self:
            # Verifica si es gerente directo de un departamento o si tiene historial de director activo
            active_director_history = employee.correspondence_director_history_ids.filtered(
                lambda h: h.date_start <= today and (not h.date_end or h.date_end >= today)
            )
            employee.is_correspondence_manager = bool(
                employee.id in managed_dept_manager_ids or active_director_history
            )

    def _search_is_correspondence_manager(self, operator, value):
        today = fields.Date.today()
        # Encuentra todos los empleados que son directores activos en el historial o gerentes de departamento
        history_director_ids = self.env['correspondence.department.director'].sudo().search([
            ('date_start', '<=', today),
            '|', ('date_end', '=', False), ('date_end', '>=', today)
        ]).mapped('director_id').ids
        dept_manager_ids = self.env['hr.department'].sudo().search([
            ('manager_id', '!=', False)
        ]).mapped('manager_id').ids
        active_directors_ids = list(set(history_director_ids + dept_manager_ids))

        if operator == '=':
            return [('id', 'in', active_directors_ids)] if value else [('id', 'not in', active_directors_ids)]
        elif operator == '!=':
            return [('id', 'not in', active_directors_ids)] if value else [('id', 'in', active_directors_ids)]
        return []