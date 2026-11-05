# -*- coding: utf-8 -*-

from odoo import models, fields, api, _

class HrDepartment(models.Model):
    _inherit = 'hr.department'

    can_receive_correspondence = fields.Boolean(
        string="Puede Recibir Correspondencia",
        default=True,
        help="Marque esta casilla si el departamento está habilitado para ser destinatario de correspondencia."
    )
    correlative_prefix = fields.Char(string='Prefijo de Correlativo', help="Prefijo usado para generar el correlativo de los documentos de este departamento (ej. 'FIN' para Finanzas).")
    director_history_ids = fields.One2many('correspondence.department.director', 'department_id', string='Historial de Directores')
    correlative_ids = fields.One2many('correspondence.department.correlative', 'department_id', string='Correlativos')
    director_signature = fields.Binary(
        related='manager_id.director_signature',
        string="Firma del Gerente / Director",
        readonly=False,
    )
    director_stamp = fields.Binary(
        related='manager_id.director_stamp',
        string="Sello del Departamento",
        readonly=False,
    )
    director_description = fields.Html(
        related='manager_id.director_description',
        string="Descripción / Gaceta del Cargo",
        readonly=False,
    )

    def _set_current_director_as_manager(self):
        """
        Identifica al gerente actual desde el historial y lo asigna como
        gerente del departamento en el campo 'manager_id'.
        """
        self.ensure_one()
        today = fields.Date.today()
        
        # Busca el director activo hoy desde el historial, ordenando por fecha de inicio descendente
        # para priorizar el más reciente si hay solapamientos (aunque idealmente no debería haberlos).
        active_histories = self.director_history_ids.filtered(
            lambda h: h.date_start <= today and (not h.date_end or h.date_end >= today)
        ).sorted(key=lambda h: h.date_start, reverse=True)
        
        current_director_employee = active_histories[0].director_id if active_histories else False

        # Si el gerente del historial es diferente al gerente actual, lo actualizamos.
        # Esto también cubre el caso de desasignar un gerente si current_director_employee es False.
        if self.manager_id != current_director_employee:
            self.manager_id = current_director_employee

    @api.model_create_multi
    def create(self, vals_list):
        # Si se crea un departamento con un manager_id, lo asignamos directamente
        # y la sincronización del historial se hará en el write si es necesario.
        # Odoo ya llama a write después de create, así que no necesitamos llamar
        # a _set_current_director_as_manager aquí si el manager_id está en vals.
        # Si no está, el manager_id será False por defecto y el historial estará vacío.
        # La sincronización bidireccional se encarga de esto.

        # Llamamos al create original
        records = super().create(vals_list)
        # Para cada nuevo departamento, sincronizamos el manager_id con el historial si existe
        for department in records:
            department._set_current_director_as_manager()
        return records

    def write(self, vals):
        """
        Sobrescribe el método write para sincronizar el historial de gerentes
        cuando el campo manager_id es modificado directamente.
        """
        # Guardamos el manager_id antiguo para comparar después de super().write()
        old_manager_by_department_id = {department.id: department.manager_id.id for department in self} if 'manager_id' in vals else {}

        # Primero, llamamos al write original para que se guarden los cambios.
        res = super(HrDepartment, self).write(vals)

        # Si el manager_id fue parte de los campos actualizados...
        if 'manager_id' in vals:
            today = fields.Date.today()

            for department in self:
                new_manager_id = department.manager_id.id # Obtenemos el nuevo manager_id después de super().write()

                # Solo procedemos si el manager_id realmente cambió para este departamento o si se desasignó
                if old_manager_by_department_id.get(department.id) != new_manager_id:
                    # En Odoo 18 se usa invalidate_recordset indicando los nombres de los campos
                    department.invalidate_recordset(fnames=['director_history_ids'])
                    # Encontramos todos los historiales activos (sin fecha de fin)
                    all_active_histories = department.director_history_ids.filtered(lambda h: not h.date_end)

                    # Cerramos todos los historiales activos que NO sean para el nuevo gerente
                    histories_to_close = all_active_histories.filtered(lambda h: h.director_id.id != new_manager_id)
                    if histories_to_close:
                        histories_to_close.with_context(bypass_manager_sync_from_department_write=True).write({'date_end': today})

                    # Verificamos si ya existe un historial activo para el nuevo gerente
                    active_history_for_new_manager = all_active_histories.filtered(lambda h: h.director_id.id == new_manager_id)

                    # Si se asignó un nuevo gerente Y NO hay ya un historial activo para él, creamos uno nuevo.
                    if new_manager_id and not active_history_for_new_manager:
                        self.env['correspondence.department.director'].with_context(bypass_manager_sync_from_department_write=True).create({
                            'department_id': department.id,
                            'director_id': new_manager_id,
                            'date_start': today,
                        })
        return res

    def unlink(self):
        """
        Sobrescribe el método unlink para validación.
        Evita eliminar departamentos si tienen correspondencia asociada.
        """
        for department in self:
            # Verificar si el departamento ha enviado correspondencia
            sent_docs = self.env['correspondence_document'].search_count([('send_department_id', '=', department.id)])
            
            # Verificar si el departamento ha recibido correspondencia
            received_docs = self.env['correspondence_document'].search_count([('recipient_department_ids', 'in', [department.id])])
            
            if sent_docs > 0 or received_docs > 0:
                raise models.UserError(
                    _("No puedes eliminar el departamento '%s' porque tiene documentos de correspondencia asociados (Enviados: %s, Recibidos: %s).\n\n"
                      "En su lugar, por favor ARCHIVA el departamento.") % (department.name, sent_docs, received_docs)
                )
        
        return super(HrDepartment, self).unlink()