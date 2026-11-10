
# -*- coding: utf-8 -*-
import base64
import io
import os
import re
import secrets
import zipfile
from html import escape, unescape
from html.parser import HTMLParser
from markupsafe import Markup
from PIL import Image, ImageDraw, ImageFont
import werkzeug.urls
from odoo import models, fields, api, _
from odoo.exceptions import UserError, AccessError
from odoo.tools.misc import file_path

def _get_recipient_department_domain(self):
    """Devuelve un dominiO."""
    domain = [('can_receive_correspondence', '=', True)]
    return domain
def _get_default_send_department(self):
    """Obtiene el departamento del usuario actual como valor por defecto."""
    return self.env.user.department_id

class correspondence_document(models.Model):
    _name = 'correspondence_document'
    _description = 'Documento de Correspondencia'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _sql_constraints = [
        ('correlative_uniq', 'unique(correlative)', 'El correlativo del documento debe ser único. Ya existe un documento con este número.'),
    ]

    company_id = fields.Many2one('res.company', string='Compañía', required=True, default=lambda self: self.env.company)
    correlative = fields.Char(string='Correlativo', required=True, copy=False, default='Nuevo', readonly=True)
    external_correlative = fields.Char(string='Correlativo Externo', copy=False)
    name = fields.Char(string='Asunto', required=True)
    date = fields.Date(string='Fecha', default=fields.Date.context_today, required=True)
    author_id = fields.Many2one('res.users', string='Autor', default=lambda self: self.env.user, required=True, readonly=True)
    send_department_id = fields.Many2one('hr.department', string="Departamento Remitente", default=_get_default_send_department)
    correspondence_type = fields.Many2one('correspondence_type', string='Tipo de Correspondencia', required=True, readonly=True, states={'draft': [('readonly', False)]})
    recipient_department_ids = fields.Many2many('hr.department', string='Departamentos Destinatarios', required=True, domain=_get_recipient_department_domain)
    descripcion = fields.Html(string='Descripción', required=True)
    observaciones = fields.Text(string='Observaciones')

    state = fields.Selection([
        ('draft', 'Borrador'),
        ('signed', 'Firmado'),
        ('received', 'Recibido'),
        ('assigned', 'Asignado'),
        ('sent', 'Enviado (Interno)'),
        ('dispatched', 'Despachado (Externo)'),
        ('replied', 'Respondido'),
    ], string='Estado', default='draft', tracking=True)
    
    correspondence_flow = fields.Selection([
        ('internal', 'Interna'),
        ('incoming', 'Entrante (Externa recibida)'),
        ('outgoing', 'Saliente (Externa enviada)'),
    ], string="Flujo de Correspondencia", default='internal', required=True, index=True, tracking=True, readonly=True, states={'draft': [('readonly', False)], 'received': [('readonly', False)]})
    
    # Campo para vincular con la entidad externa (res.partner)
    external_partner_id = fields.Many2one('res.partner', string="Entidad Externa")
    external_partner_ids = fields.Many2many(
        'res.partner',
        'correspondence_doc_ext_partner_rel',
        'document_id',
        'partner_id',
        string="Destinatarios Externos",
        help="Permite dirigir el oficio a uno o varios contactos/instituciones externas."
    )
    # Campo relacionado para mostrar la dirección del partner, útil en los reportes.
    external_address = fields.Char(string="Dirección Externa", related='external_partner_id.contact_address', readonly=True)

    # Vocativo y tratamiento de destinatarios en Oficios
    recipient_treatment = fields.Selection([
        ('ciudadano', 'Ciudadano:'),
        ('ciudadana', 'Ciudadana:'),
        ('ciudadanos', 'Ciudadanos:'),
        ('ciudadanas', 'Ciudadanas:'),
    ], string="Vocativo Destinatario", default='ciudadano', required=True)

    recipient_closing = fields.Selection([
        ('su_despacho', 'Su Despacho. -'),
        ('presente', 'Presente. -'),
        ('sus_manos', 'En sus manos. -'),
    ], string="Cierre Destinatario", default='su_despacho', required=True)

    include_recipient_title = fields.Boolean(
        string="Incluir Título Profesional en Destinatario",
        default=False,
        help="Por norma de Dirección Ejecutiva se omiten títulos profesionales (Lic., Ing., etc.) salvo para autoridades de alto nivel."
    )

    # Campos "Con Copia" (C.c.)
    cc_department_ids = fields.Many2many(
        'hr.department',
        'correspondence_doc_cc_dept_rel',
        'document_id',
        'department_id',
        string="Con Copia (Departamentos / Coordinaciones)",
        domain=_get_recipient_department_domain
    )
    cc_partner_ids = fields.Many2many(
        'res.partner',
        'correspondence_doc_cc_partner_rel',
        'document_id',
        'partner_id',
        string="Con Copia (Contactos Externos)"
    )

    # Saludo y Despedida Institucional Fijos (Bloqueados para usuarios estándar)
    use_institutional_greeting = fields.Boolean(
        string="Incluir Saludo y Despedida Institucional Fijos",
        default=True,
        help="Inserta automáticamente el saludo y la despedida oficial de Presidencia/CENIPA sin permitir alteraciones."
    )
    institutional_greeting = fields.Html(
        related='correspondence_type.default_greeting',
        string="Saludo Institucional Oficial (Fijo)",
        readonly=True
    )
    institutional_farewell = fields.Html(
        related='correspondence_type.default_farewell',
        string="Despedida Institucional Oficial (Fija)",
        readonly=True
    )

    # Pestaña de Anexos Impresos (Posterior a la Firma) y Numeración de Páginas
    tiene_anexos = fields.Boolean(
        string="¿Contiene Anexos Impresos?",
        default=False,
        help="Activa la pestaña de Anexos que se imprimirán en páginas posteriores a la firma del documento manteniendo cintillo y correlativo."
    )
    anexos_html = fields.Html(
        string="Contenido de Anexos (Posterior a la Firma)",
        sanitize=False,
        help="Tablas, matrices, informes o imágenes (Cédula/RIF) que se imprimirán después del bloque de firma."
    )
    show_page_numbers = fields.Boolean(
        string="Mostrar Numeración de Páginas",
        default=True
    )
    manual_total_pages = fields.Integer(
        string="Total de Páginas Manual (0 = Automático)",
        default=0,
        help="Si es 0, el sistema calcula el total de páginas automáticamente (ej. 1 de 1). Si indica un número mayor a 0, se usará como total (ej. 1 de 20) cuando anexe hojas físicas externas."
    )

    # Exportación Editable (.docx)
    docx_file = fields.Binary(string="Archivo Word Editable (.docx)", attachment=True, copy=False)
    docx_filename = fields.Char(string="Nombre Archivo Editable", copy=False)


    # Campo para correspondencia saliente
    shipping_method = fields.Selection([
        ('email', 'Correo Electrónico'),
        ('courier', 'Courier / Mensajería'),
        ('certified_mail', 'Correo Certificado'),
        ('in_person', 'Entrega en Persona'),
    ], string="Método de Envío")
    
    physical_location_id = fields.Many2one('correspondence.physical.location', string='Ubicación Física', tracking=True, copy=False)

    read_status_ids = fields.One2many('correspondence.document.read_status', 'document_id', string='Estados de Lectura')
    
    is_current_user_recipient = fields.Boolean(
        string="¿Usuario actual es destinatario?", compute='_compute_is_current_user_recipient')
    
    already_read_by_my_department = fields.Boolean(
        string="¿Ya leído por mi departamento?", compute='_compute_is_current_user_recipient')

    director_has_digital_signature = fields.Boolean(
        string="¿Director tiene firma digital?", compute='_compute_director_has_digital_signature')

    user_facing_state = fields.Char(
        string="Estado (Usuario)", compute='_compute_user_facing_state')

    public_url = fields.Char(
        string="URL Pública", compute='_compute_public_url', help="URL para la verificación pública del documento.")
    
    access_token = fields.Char(
        string='Token de Acceso', compute='_compute_access_token', store=True, precompute=True, copy=False, readonly=True
    )
    
    date_formatted_spanish = fields.Char(
        string="Fecha Formateada (Mayúsculas)", compute="_compute_formatted_dates")
    date_formatted_spanish_long = fields.Char(
        string="Fecha Larga Formateada", compute="_compute_formatted_dates")
    author_initials = fields.Char(
        string="Iniciales", compute="_compute_author_initials")

    @api.onchange('correspondence_type', 'use_institutional_greeting')
    def _onchange_correspondence_type_template(self):
        if self.correspondence_type and not self.descripcion and not self.use_institutional_greeting:
            greeting = self.correspondence_type.default_greeting or ''
            farewell = self.correspondence_type.default_farewell or ''
            self.descripcion = f"{greeting}<p><br/></p>{farewell}"

    @api.onchange('external_partner_ids', 'external_partner_id', 'recipient_department_ids')
    def _onchange_recipients_sync_treatment(self):
        if self.external_partner_ids and not self.external_partner_id:
            self.external_partner_id = self.external_partner_ids[0]
        elif self.external_partner_ids and self.external_partner_id not in self.external_partner_ids:
            self.external_partner_id = self.external_partner_ids[0]

        total_rec = len(self.external_partner_ids) if self.external_partner_ids else (1 if self.external_partner_id else len(self.recipient_department_ids))
        if total_rec > 1:
            if self.recipient_treatment == 'ciudadano':
                self.recipient_treatment = 'ciudadanos'
            elif self.recipient_treatment == 'ciudadana':
                self.recipient_treatment = 'ciudadanas'
        elif total_rec == 1:
            if self.recipient_treatment == 'ciudadanos':
                self.recipient_treatment = 'ciudadano'
            elif self.recipient_treatment == 'ciudadanas':
                self.recipient_treatment = 'ciudadana'

    def _strip_professional_title(self, full_name):
        """
        Limpia prefijos profesionales (Lic., Ing., Dr., MSc., etc.) cuando
        include_recipient_title=False y retorna el nombre en MAYÚSCULAS.
        """
        if not full_name:
            return ""
        text = str(full_name).strip().upper()
        if not self.include_recipient_title:
            text = re.sub(
                r'^(LICDO\.?|LICDA\.?|LIC\.?|ING\.?|DR\.?|DRA\.?|MSC\.?|MGSC\.?|ESP\.?|TSU\.?|ABG\.?|ABOG\.?|ECON\.?|ADM\.?|PROF\.?|PROFA\.?|CNEL\.?|GRAL\.?|CAP\.?|TTE\.?)\s+',
                '',
                text,
                flags=re.IGNORECASE,
            ).strip()
        return text

    def get_recipient_treatment_label(self):
        self.ensure_one()
        labels = {
            'ciudadano': 'Ciudadano:',
            'ciudadana': 'Ciudadana:',
            'ciudadanos': 'Ciudadanos:',
            'ciudadanas': 'Ciudadanas:',
        }
        return labels.get(self.recipient_treatment or 'ciudadano', 'Ciudadano:')

    def get_recipient_closing_label(self):
        self.ensure_one()
        labels = {
            'su_despacho': 'Su Despacho. -',
            'presente': 'Presente. -',
            'sus_manos': 'En sus manos. -',
        }
        return labels.get(self.recipient_closing or 'su_despacho', 'Su Despacho. -')

    def get_formatted_recipients_list(self):
        """
        Devuelve la lista estructurada de destinatarios (externos o internos)
        en MAYÚSCULAS con Nombre, Cargo e Institución para Oficios, Memorandos y Circulares.
        """
        self.ensure_one()
        recipients = []
        partners = self.external_partner_ids or self.external_partner_id
        if partners:
            for p in partners:
                name_clean = self._strip_professional_title(p.name)
                job_clean = (p.function or '').strip().upper()
                inst_clean = (p.parent_id.name if p.parent_id else (p.company_name or '')).strip().upper()
                addr_clean = (p.street or '').strip()
                recipients.append({
                    'name': name_clean,
                    'job': job_clean,
                    'institution': inst_clean,
                    'address': addr_clean,
                })
        if self.recipient_department_ids and (not partners or self.correspondence_flow == 'internal'):
            for dept in self.recipient_department_ids:
                mgr = dept.manager_id
                name_clean = self._strip_professional_title(mgr.name if mgr else dept.name)
                job_clean = (mgr.job_id.name if (mgr and mgr.job_id) else dept.name or '').strip().upper()
                inst_clean = "CENTRO NACIONAL DE INVESTIGACIÓN DE PESCA Y ACUICULTURA (CENIPA)"
                recipients.append({
                    'name': name_clean,
                    'job': job_clean,
                    'institution': inst_clean,
                    'address': '',
                })
        return recipients

    def get_formatted_cc_list(self):
        """
        Devuelve la lista de destinatarios 'Con Copia' (C.c.) internos y externos
        con Nombre, Cargo e Institución en MAYÚSCULAS.
        """
        self.ensure_one()
        cc_list = []
        for dept in self.cc_department_ids:
            mgr = dept.manager_id
            name_clean = self._strip_professional_title(mgr.name if mgr else dept.name)
            job_clean = (mgr.job_id.name if (mgr and mgr.job_id) else dept.name or '').strip().upper()
            cc_list.append({
                'name': name_clean,
                'job': job_clean,
                'institution': "CENTRO NACIONAL DE INVESTIGACIÓN DE PESCA Y ACUICULTURA (CENIPA)",
            })
        for p in self.cc_partner_ids:
            name_clean = self._strip_professional_title(p.name)
            job_clean = (p.function or '').strip().upper()
            inst_clean = (p.parent_id.name if p.parent_id else (p.company_name or '')).strip().upper()
            cc_list.append({
                'name': name_clean,
                'job': job_clean,
                'institution': inst_clean,
            })
        return cc_list

    def get_full_body_html(self):
        """
        Retorna el cuerpo HTML completo como objeto Markup (para renderizado directo en QWeb t-out)
        inyectando el saludo y la despedida institucional fijos (cuando use_institutional_greeting=True)
        sin duplicarlos si ya existían en descripcion, y limpiando párrafos vacíos (<p>&nbsp;</p>).
        """
        self.ensure_one()

        def normalize_html_paragraphs(raw_html):
            if not raw_html:
                return ""
            cleaned = str(raw_html).strip()
            cleaned = re.sub(r'<div(\b[^>]*)>', r'<p\1>', cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r'</div>', '</p>', cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(
                r'<p[^>]*>(?:\s|&nbsp;|\u00a0|<br\s*/?>)*</p>',
                '',
                cleaned,
                flags=re.IGNORECASE,
            )
            return cleaned.strip()

        body = normalize_html_paragraphs(self.descripcion)
        if not self.use_institutional_greeting or not self.correspondence_type:
            return Markup(body)

        greeting = normalize_html_paragraphs(self.correspondence_type.default_greeting)
        farewell = normalize_html_paragraphs(self.correspondence_type.default_farewell)

        def plain_text(html_fragment):
            return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', unescape(html_fragment or ''))).strip().lower()

        body_plain = plain_text(body)
        greet_plain = plain_text(greeting)
        farewell_plain = plain_text(farewell)

        parts = []
        if greeting and (not greet_plain or greet_plain[:45] not in body_plain):
            parts.append(greeting)
        if body:
            parts.append(body)
        if farewell and (not farewell_plain or farewell_plain[:35] not in body_plain):
            parts.append(farewell)
        return Markup("\n".join(parts))

    @api.depends('date')
    def _compute_formatted_dates(self):
        months_upper = ['ENERO', 'FEBRERO', 'MARZO', 'ABRIL', 'MAYO', 'JUNIO',
                        'JULIO', 'AGOSTO', 'SEPTIEMBRE', 'OCTUBRE', 'NOVIEMBRE', 'DICIEMBRE']
        months_lower = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
                        'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']
        for doc in self:
            if doc.date:
                day = doc.date.strftime('%d')
                month_idx = doc.date.month - 1
                year = doc.date.strftime('%Y')
                doc.date_formatted_spanish = f"{day} DE {months_upper[month_idx]} DE {year}"
                doc.date_formatted_spanish_long = f"Caracas, {day} de {months_lower[month_idx]} de {year}"
            else:
                doc.date_formatted_spanish = ""
                doc.date_formatted_spanish_long = ""

    @api.depends('send_department_id', 'author_id')
    def _compute_author_initials(self):
        for doc in self:
            manager_name = doc.send_department_id.manager_id.name or doc.author_id.name or ""
            author_name = doc.author_id.name or ""
            
            def get_initials(text):
                parts = [p[0] for p in text.strip().split() if p]
                return "".join(parts)
            
            upper_init = get_initials(manager_name).upper() or "XX"
            lower_init = get_initials(author_name).lower() or "xx"
            doc.author_initials = f"{upper_init}/{lower_init}"

    @api.depends('create_date')
    def _compute_access_token(self):
        for doc in self:
            if not doc.access_token:
                doc.access_token = ''.join(secrets.token_urlsafe(32))

    @api.depends('author_id', 'author_id.employee_id.department_id')
    def _compute_send_department_id(self):
        """
        Establece el departamento del autor como valor por defecto,
        pero permite que sea modificado.
        """
        for doc in self:
            doc.send_department_id = doc.author_id.employee_id.department_id

    @api.depends('recipient_department_ids', 'read_status_ids.department_id')
    def _compute_is_current_user_recipient(self):
        for doc in self:
            user_department = self.env.user.employee_id.department_id
            doc.is_current_user_recipient = user_department in doc.recipient_department_ids
            doc.already_read_by_my_department = user_department in doc.read_status_ids.mapped('department_id')

    @api.depends('send_department_id', 'send_department_id.manager_id.director_signature', 'send_department_id.manager_id.director_stamp')
    def _compute_director_has_digital_signature(self):
        for doc in self:
            manager = doc.send_department_id.manager_id
            doc.director_has_digital_signature = bool(manager and manager.director_signature and manager.director_stamp)

    # Campo auxiliar para manejar el dominio desde la vista XML
    excluded_department_id = fields.Many2one('hr.department', compute='_compute_excluded_department_id')

    @api.depends('correspondence_flow', 'send_department_id')
    def _compute_excluded_department_id(self):
        for doc in self:
            if doc.correspondence_flow == 'incoming':
                # Si es entrante, no excluimos nada (False permite todo en '!=')
                doc.excluded_department_id = False
            else:
                # Si es interna/saliente, excluimos el departamento remitente
                doc.excluded_department_id = doc.send_department_id

    @api.depends('state', 'author_id', 'recipient_department_ids')
    def _compute_user_facing_state(self):
        """
        Calcula la etiqueta del estado que se mostrará al usuario actual
        dependiendo de si es el autor o un destinatario.
        """
        for doc in self:
            is_author = doc.author_id == self.env.user
            is_recipient = self.env.user.employee_id.department_id in doc.recipient_department_ids

            if doc.state == 'sent':
                if is_author:
                    doc.user_facing_state = _('Enviado')
                elif is_recipient:
                    doc.user_facing_state = _('Recibido')
                else:
                    doc.user_facing_state = _('En Gestión') # Fallback para otros usuarios (ej. admin)
            else:
                doc.user_facing_state = doc.get_state_display_name()

    # Campos Snapshot para persistencia histórica de nombres
    send_department_static = fields.Char(string="Departamento Remitente (Histórico)", readonly=True)
    recipient_departments_static = fields.Char(string="Departamentos Destinatarios (Histórico)", readonly=True)

    def action_sign(self):
        self.ensure_one()

        # 1. Obtener la acción de reporte
        report_action = self.correspondence_type.report_action_id or self.env.ref('correspondence.action_report_correspondence_document')
        
        # 2. Generar el PDF
        # ¡IMPORTANTE!: No uses "_" aquí. Usa "report_type" o similar.
        pdf_content, report_type = report_action._render_qweb_pdf(report_action.id, res_ids=self.ids)

        # 3. Nombre del archivo
        file_name = f"{self.correlative or 'Documento'}.pdf"
        
        # 3.5. SNAPSHOT: Guardamos los nombres de los departamentos en este momento exacto
        # Esto asegura que si se renombra el departamento después, este documento mantenga el nombre original.
        static_send = self.send_department_id.name
        static_recipients = ", ".join(self.recipient_department_ids.mapped('name'))

        # 4. Guardar y actualizar
        self.write({
            'document_file': base64.b64encode(pdf_content),
            'file_name': file_name,
            'state': 'signed',
            'send_department_static': static_send,
            'recipient_departments_static': static_recipients,
        })

        # 5. Ahora "_" seguirá siendo la función de traducción importada arriba
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Correcto'),
                'message': _('Documento firmado con éxito'),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            }
        }

    def action_regenerate_pdf(self):
        """Regenera el archivo PDF del documento con la plantilla y sellos vigentes."""
        self.ensure_one()
        report_action = self.correspondence_type.report_action_id or self.env.ref('correspondence.action_report_correspondence_document')
        pdf_content, _report_type = report_action._render_qweb_pdf(report_action.id, res_ids=self.ids)
        file_name = f"{self.correlative or 'Documento'}.pdf"
        vals = {
            'document_file': base64.b64encode(pdf_content),
            'file_name': file_name,
        }
        if self.state != 'draft':
            vals['send_department_static'] = self.send_department_id.name
            vals['recipient_departments_static'] = ", ".join(self.recipient_department_ids.mapped('name'))
        self.write(vals)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('PDF Regenerado'),
                'message': _('El documento PDF ha sido regenerado y actualizado correctamente.'),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            }
        }

    def action_reset_to_draft(self):
        """
        Devuelve el documento al estado Borrador (o Recibido si es entrante)
        conservando su correlativo para permitir su modificación o reutilización autorizada.
        Restringido exclusivamente a Directores y Administradores de Correspondencia.
        """
        user = self.env.user
        is_authorized = (
            user.has_group('correspondence.correspondence_group_director')
            or user.has_group('correspondence.correspondence_group_admin')
            or user.has_group('base.group_system')
        )
        if not is_authorized:
            raise AccessError(_(
                "Solo el personal autorizado de Dirección Ejecutiva / Directores puede devolver un documento aprobado a borrador o reutilizar su correlativo."
            ))

        for doc in self:
            target_state = 'received' if doc.correspondence_flow == 'incoming' else 'draft'
            doc.write({
                'state': target_state,
                'send_department_static': False,
                'recipient_departments_static': False,
            })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Devuelto a Borrador (Correlativo Preservado)'),
                'message': _('El documento fue devuelto a borrador conservando su número correlativo. Ahora puede modificar fecha, destinatarios, asunto y contenido.'),
                'type': 'info',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            }
        }

    def _html_to_ooxml_elements(self, html_content):
        """
        Convierte un fragmento HTML (párrafos y tablas) en bloques XML nativos de Word (w:p y w:tbl)
        con fuente Georama 11pt, sin interlineado excesivo, separación entre párrafos y tablas con
        encabezado azul institucional (#002060).
        """
        if not html_content:
            return ""

        class _OOXMLHTMLParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.elements = []
                self.in_table = False
                self.table_rows = []
                self.current_row = []
                self.in_cell = False
                self.cell_is_header = False
                self.current_cell_text = []
                self.current_p_runs = []
                self.bold_stack = 0
                self.italic_stack = 0
                self.underline_stack = 0

            def _flush_paragraph(self, align="both"):
                text_check = "".join(t for t, _b, _i, _u in self.current_p_runs).strip()
                if not text_check:
                    self.current_p_runs = []
                    return
                runs_xml = []
                for txt, is_b, is_i, is_u in self.current_p_runs:
                    if not txt:
                        continue
                    rpr = [
                        '<w:rFonts w:ascii="Georama" w:hAnsi="Georama" w:cs="Georama"/>',
                        '<w:sz w:val="22"/><w:szCs w:val="22"/>',
                    ]
                    if is_b:
                        rpr.append('<w:b/>')
                    if is_i:
                        rpr.append('<w:i/>')
                    if is_u:
                        rpr.append('<w:u w:val="single"/>')
                    runs_xml.append(
                        f'<w:r><w:rPr>{"".join(rpr)}</w:rPr>'
                        f'<w:t xml:space="preserve">{escape(txt)}</w:t></w:r>'
                    )
                p_xml = (
                    f'<w:p><w:pPr><w:spacing w:before="0" w:after="160" w:line="240" w:lineRule="auto"/>'
                    f'<w:jc w:val="{align}"/></w:pPr>{"".join(runs_xml)}</w:p>'
                )
                self.elements.append(p_xml)
                self.current_p_runs = []

            def handle_starttag(self, tag, attrs):
                t = tag.lower()
                if t == 'table':
                    self._flush_paragraph()
                    self.in_table = True
                    self.table_rows = []
                elif t == 'tr' and self.in_table:
                    self.current_row = []
                elif t in ('th', 'td') and self.in_table:
                    self.in_cell = True
                    self.cell_is_header = (t == 'th')
                    self.current_cell_text = []
                elif t in ('b', 'strong'):
                    self.bold_stack += 1
                elif t in ('i', 'em'):
                    self.italic_stack += 1
                elif t == 'u':
                    self.underline_stack += 1
                elif t == 'br':
                    if self.in_cell:
                        self.current_cell_text.append(" ")
                    else:
                        self._flush_paragraph()
                elif t in ('p', 'div', 'li', 'h1', 'h2', 'h3', 'h4'):
                    if not self.in_table:
                        self._flush_paragraph()

            def handle_endtag(self, tag):
                t = tag.lower()
                if t in ('b', 'strong'):
                    self.bold_stack = max(0, self.bold_stack - 1)
                elif t in ('i', 'em'):
                    self.italic_stack = max(0, self.italic_stack - 1)
                elif t == 'u':
                    self.underline_stack = max(0, self.underline_stack - 1)
                elif t in ('th', 'td') and self.in_cell:
                    cell_txt = " ".join("".join(self.current_cell_text).split())
                    self.current_row.append((cell_txt, self.cell_is_header))
                    self.in_cell = False
                elif t == 'tr' and self.in_table:
                    if self.current_row:
                        self.table_rows.append(self.current_row)
                    self.current_row = []
                elif t == 'table' and self.in_table:
                    self.in_table = False
                    if self.table_rows:
                        rows_xml = []
                        for r_idx, row in enumerate(self.table_rows):
                            is_hdr_row = (r_idx == 0) or all(is_h for _c, is_h in row)
                            cells_xml = []
                            for cell_txt, is_h in row:
                                hdr = is_hdr_row or is_h
                                fill = "002060" if hdr else "FFFFFF"
                                color = "FFFFFF" if hdr else "000000"
                                b_tag = "<w:b/>" if hdr else ""
                                jc = "center" if hdr else "left"
                                cells_xml.append(
                                    f'<w:tc><w:tcPr><w:shd w:val="clear" w:color="auto" w:fill="{fill}"/>'
                                    f'<w:tcMar><w:top w:w="80" w:type="dxa"/><w:bottom w:w="80" w:type="dxa"/>'
                                    f'<w:left w:w="120" w:type="dxa"/><w:right w:w="120" w:type="dxa"/></w:tcMar>'
                                    f'</w:tcPr><w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/>'
                                    f'<w:jc w:val="{jc}"/></w:pPr><w:r><w:rPr>'
                                    f'<w:rFonts w:ascii="Georama" w:hAnsi="Georama" w:cs="Georama"/>'
                                    f'<w:sz w:val="20"/><w:szCs w:val="20"/><w:color w:val="{color}"/>{b_tag}'
                                    f'</w:rPr><w:t xml:space="preserve">{escape(cell_txt)}</w:t></w:r></w:p></w:tc>'
                                )
                            rows_xml.append(f'<w:tr><w:trPr><w:cantSplit/></w:trPr>{"".join(cells_xml)}</w:tr>')
                        tbl_xml = (
                            '<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/>'
                            '<w:tblBorders>'
                            '<w:top w:val="single" w:sz="6" w:space="0" w:color="002060"/>'
                            '<w:left w:val="single" w:sz="6" w:space="0" w:color="333333"/>'
                            '<w:bottom w:val="single" w:sz="6" w:space="0" w:color="333333"/>'
                            '<w:right w:val="single" w:sz="6" w:space="0" w:color="333333"/>'
                            '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="333333"/>'
                            '<w:insideV w:val="single" w:sz="4" w:space="0" w:color="333333"/>'
                            '</w:tblBorders></w:tblPr>'
                            f'{"".join(rows_xml)}</w:tbl>'
                            '<w:p><w:pPr><w:spacing w:before="0" w:after="120"/></w:pPr></w:p>'
                        )
                        self.elements.append(tbl_xml)
                elif t in ('p', 'div', 'li', 'h1', 'h2', 'h3', 'h4'):
                    if not self.in_table:
                        self._flush_paragraph()

            def handle_data(self, data):
                if not data:
                    return
                if self.in_cell:
                    self.current_cell_text.append(data)
                else:
                    clean = re.sub(r'\s+', ' ', data)
                    if clean:
                        self.current_p_runs.append((
                            clean,
                            self.bold_stack > 0,
                            self.italic_stack > 0,
                            self.underline_stack > 0,
                        ))

        parser = _OOXMLHTMLParser()
        parser.feed(str(html_content))
        parser._flush_paragraph()
        return "".join(parser.elements)

    def _build_header_png_bytes(self):
        """
        Compone en memoria la imagen del cintillo superior institucional (1920x200 PNG)
        idéntica al encabezado PDF: logo-izquierda.png (Ministerio + CENIPA) a la izquierda
        y logo-derecha.png (Bicentenario) a la derecha con las proporciones exactas de QWeb.
        """
        canvas = Image.new("RGBA", (1920, 200), (255, 255, 255, 255))
        left_img = None
        if self.company_id and self.company_id.logo:
            try:
                left_img = Image.open(io.BytesIO(base64.b64decode(self.company_id.logo))).convert("RGBA")
            except Exception:
                left_img = None
        if left_img is None:
            try:
                left_path = file_path('correspondence/static/src/img/logo-izquierda.png')
                if left_path and os.path.exists(left_path):
                    left_img = Image.open(left_path).convert("RGBA")
            except Exception:
                left_img = None

        if left_img is not None:
            target_h = 140
            ratio = target_h / float(left_img.height or 1)
            target_w = max(1, int(left_img.width * ratio))
            left_resized = left_img.resize((target_w, target_h), Image.Resampling.LANCZOS)
            canvas.paste(left_resized, (0, 30), left_resized)

        try:
            right_path = file_path('correspondence/static/src/img/logo-derecha.png')
            if right_path and os.path.exists(right_path):
                right_img = Image.open(right_path).convert("RGBA")
                target_h = 148
                ratio = target_h / float(right_img.height or 1)
                target_w = max(1, int(right_img.width * ratio))
                right_resized = right_img.resize((target_w, target_h), Image.Resampling.LANCZOS)
                canvas.paste(right_resized, (max(0, 1920 - target_w), 26), right_resized)
        except Exception:
            pass

        out = io.BytesIO()
        canvas.convert("RGB").save(out, format="PNG")
        return out.getvalue()

    def _build_footer_png_bytes(self):
        """
        Compone en memoria la imagen del pie de página institucional (1920x405 PNG)
        idéntica al PDF QWeb: cintillo de ondas (footer.png), tarjeta QR blanca redondeada,
        íconos circulares negros (teléfono, Instagram, Facebook, TikTok, YouTube, ubicación),
        textos de contacto dentro de la onda amarilla y pastilla blanca 'Página X de Y' a la derecha.
        """
        canvas = Image.new("RGBA", (1920, 405), (255, 255, 255, 255))
        try:
            footer_path = file_path('correspondence/static/src/img/footer.png')
            if footer_path and os.path.exists(footer_path):
                f_img = Image.open(footer_path).convert("RGBA").resize((1920, 405), Image.Resampling.LANCZOS)
                canvas.paste(f_img, (0, 0), f_img)
        except Exception:
            pass

        draw = ImageDraw.Draw(canvas)
        qr_data_uri = self.get_qr_code_url()
        if qr_data_uri and "," in qr_data_uri:
            try:
                qr_raw = base64.b64decode(qr_data_uri.split(",", 1)[1])
                qr_img = Image.open(io.BytesIO(qr_raw)).convert("RGBA").resize((164, 164), Image.Resampling.LANCZOS)
                draw.rounded_rectangle([(52, 200), (236, 384)], radius=22, fill=(255, 255, 255, 255))
                canvas.paste(qr_img, (62, 210), qr_img)
            except Exception:
                pass

        font_bold = None
        font_addr = None
        font_page = None
        font_icon = None
        for font_candidate in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ):
            if os.path.exists(font_candidate):
                try:
                    font_bold = ImageFont.truetype(font_candidate, 18)
                    font_addr = ImageFont.truetype(font_candidate, 14)
                    font_page = ImageFont.truetype(font_candidate, 18)
                    font_icon = ImageFont.truetype(font_candidate, 15)
                    break
                except Exception:
                    pass
        if font_bold is None:
            font_bold = ImageFont.load_default()
            font_addr = ImageFont.load_default()
            font_page = ImageFont.load_default()
            font_icon = ImageFont.load_default()

        # Fila 1: Ícono circular de Teléfono + número
        cx1, cy1 = 256, 268
        draw.ellipse([(cx1, cy1), (cx1 + 26, cy1 + 26)], fill=(0, 0, 0, 255))
        draw.arc([(cx1 + 6, cy1 + 6), (cx1 + 20, cy1 + 20)], start=110, end=340, fill=(255, 255, 255, 255), width=3)
        draw.text((cx1 + 34, cy1 + 3), "0212-5072911 ext 8049", fill=(0, 0, 0, 255), font=font_bold)

        # Fila 2: 4 Íconos circulares de Redes Sociales (Instagram, Facebook, TikTok, YouTube) + @cenipave
        cy2 = 302
        # 1. Instagram
        ix1 = 256
        draw.ellipse([(ix1, cy2), (ix1 + 26, cy2 + 26)], fill=(0, 0, 0, 255))
        draw.rounded_rectangle([(ix1 + 6, cy2 + 6), (ix1 + 20, cy2 + 20)], radius=4, outline=(255, 255, 255, 255), width=2)
        draw.ellipse([(ix1 + 10, cy2 + 10), (ix1 + 16, cy2 + 16)], outline=(255, 255, 255, 255), width=2)
        # 2. Facebook
        ix2 = 286
        draw.ellipse([(ix2, cy2), (ix2 + 26, cy2 + 26)], fill=(0, 0, 0, 255))
        draw.text((ix2 + 9, cy2 + 4), "f", fill=(255, 255, 255, 255), font=font_icon)
        # 3. TikTok
        ix3 = 316
        draw.ellipse([(ix3, cy2), (ix3 + 26, cy2 + 26)], fill=(0, 0, 0, 255))
        draw.line([(ix3 + 14, cy2 + 7), (ix3 + 14, cy2 + 18)], fill=(255, 255, 255, 255), width=2)
        draw.ellipse([(ix3 + 9, cy2 + 15), (ix3 + 14, cy2 + 20)], fill=(255, 255, 255, 255))
        draw.arc([(ix3 + 14, cy2 + 6), (ix3 + 20, cy2 + 12)], start=0, end=120, fill=(255, 255, 255, 255), width=2)
        # 4. YouTube
        ix4 = 346
        draw.ellipse([(ix4, cy2), (ix4 + 26, cy2 + 26)], fill=(0, 0, 0, 255))
        draw.polygon([(ix4 + 10, cy2 + 8), (ix4 + 10, cy2 + 18), (ix4 + 19, cy2 + 13)], fill=(255, 255, 255, 255))
        draw.text((ix4 + 34, cy2 + 3), "@cenipave", fill=(0, 0, 0, 255), font=font_bold)

        # Fila 3: Ícono de Ubicación + Dirección en 2 líneas dentro de la franja amarilla
        cy3 = 338
        draw.ellipse([(260, cy3 + 2), (278, cy3 + 20)], outline=(0, 0, 0, 255), width=3)
        draw.polygon([(261, cy3 + 15), (277, cy3 + 15), (269, cy3 + 27)], fill=(0, 0, 0, 255))
        draw.ellipse([(266, cy3 + 8), (272, cy3 + 14)], fill=(255, 210, 0, 255))
        draw.text(
            (288, cy3),
            "Caracas, Dto. Metropolitano, Municipio Libertador, Av. Lecuna, Parque",
            fill=(0, 0, 0, 255),
            font=font_addr,
        )
        draw.text(
            (288, cy3 + 18),
            "Central, Torre Este, Piso 22",
            fill=(0, 0, 0, 255),
            font=font_addr,
        )

        # Pastilla de Numeración de Página en esquina inferior derecha (idéntica al PDF)
        if self.show_page_numbers:
            total_pg = self.manual_total_pages if (self.manual_total_pages and self.manual_total_pages > 0) else (2 if (self.tiene_anexos and self.anexos_html) else 1)
            pg_label = f"Página 1 de {total_pg}"
            draw.rounded_rectangle([(1620, 344), (1798, 384)], radius=12, fill=(255, 255, 255, 242))
            draw.text((1642, 353), pg_label, fill=(0, 32, 96, 255), font=font_page)

        out = io.BytesIO()
        canvas.convert("RGB").save(out, format="PNG")
        return out.getvalue()

    def _build_signature_png_bytes(self):
        """
        Compone en memoria el bloque visual de Sello (izquierda) y Firma (derecha) del Director
        (1080x290 PNG) para incrustarlo justo encima del nombre del firmante, igual que en el PDF.
        Retorna None si el departamento no tiene ni firma ni sello cargados.
        """
        mgr = self.send_department_id.manager_id
        stamp_b64 = mgr.director_stamp if mgr else False
        sig_b64 = mgr.director_signature if mgr else False
        if not stamp_b64 and not sig_b64:
            return None

        canvas = Image.new("RGBA", (1080, 290), (255, 255, 255, 255))
        if stamp_b64:
            try:
                s_img = Image.open(io.BytesIO(base64.b64decode(stamp_b64))).convert("RGBA")
                target_h = 270
                ratio = target_h / float(s_img.height or 1)
                target_w = max(1, int(s_img.width * ratio))
                s_res = s_img.resize((target_w, target_h), Image.Resampling.LANCZOS)
                canvas.paste(s_res, (max(0, 270 - target_w // 2), 10), s_res)
            except Exception:
                pass
        if sig_b64:
            try:
                g_img = Image.open(io.BytesIO(base64.b64decode(sig_b64))).convert("RGBA")
                target_h = 260
                ratio = target_h / float(g_img.height or 1)
                target_w = max(1, int(g_img.width * ratio))
                g_res = g_img.resize((target_w, target_h), Image.Resampling.LANCZOS)
                canvas.paste(g_res, (max(540, 810 - target_w // 2), 15), g_res)
            except Exception:
                pass

        out = io.BytesIO()
        canvas.convert("RGB").save(out, format="PNG")
        return out.getvalue()

    def _build_docx_bytes(self):
        """
        Construye en memoria un archivo Word (.docx) OpenXML nativo con paridad visual 1:1
        respecto al PDF oficial CENIPA (Encabezado a 10mm del borde, Pie de página sangrado
        completo con ondas/QR/íconos/pastilla de página, fila de correlativo y fecha,
        destinatarios en azul #002060, cuerpo Georama 11pt, imagen de sello+firma, Gaceta,
        sección Con Copia y salto de página para Anexos).
        """
        self.ensure_one()

        def make_p(text, bold=False, italic=False, underline=False, color="000000", sz=22, align="left", after=60, before=0, left_ind=0, right_ind=0):
            rpr = [
                '<w:rFonts w:ascii="Georama" w:hAnsi="Georama" w:cs="Georama"/>',
                f'<w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/>',
                f'<w:color w:val="{color}"/>',
            ]
            if bold:
                rpr.append('<w:b/>')
            if italic:
                rpr.append('<w:i/>')
            if underline:
                rpr.append('<w:u w:val="single"/>')
            ind_xml = f'<w:ind w:left="{left_ind}" w:right="{right_ind}"/>' if (left_ind or right_ind) else ''
            return (
                f'<w:p><w:pPr><w:spacing w:before="{before}" w:after="{after}" w:line="240" w:lineRule="auto"/>'
                f'{ind_xml}<w:jc w:val="{align}"/></w:pPr>'
                f'<w:r><w:rPr>{"".join(rpr)}</w:rPr><w:t xml:space="preserve">{escape(str(text or ""))}</w:t></w:r></w:p>'
            )

        def make_drawing_xml(rel_id, doc_pr_id, name, cx, cy):
            return (
                '<w:drawing>'
                '<wp:inline distT="0" distB="0" distL="0" distR="0" '
                'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                f'<wp:extent cx="{cx}" cy="{cy}"/>'
                f'<wp:docPr id="{doc_pr_id}" name="{name}"/>'
                '<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
                '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
                '<pic:pic>'
                f'<pic:nvPicPr><pic:cNvPr id="{doc_pr_id}" name="{name}"/><pic:cNvPicPr/></pic:nvPicPr>'
                f'<pic:blipFill><a:blip r:embed="{rel_id}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
                f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
                '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
                '</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing>'
            )

        no_borders_xml = (
            '<w:tblBorders>'
            '<w:top w:val="none" w:sz="0" w:space="0" w:color="auto"/>'
            '<w:left w:val="none" w:sz="0" w:space="0" w:color="auto"/>'
            '<w:bottom w:val="none" w:sz="0" w:space="0" w:color="auto"/>'
            '<w:right w:val="none" w:sz="0" w:space="0" w:color="auto"/>'
            '<w:insideH w:val="none" w:sz="0" w:space="0" w:color="auto"/>'
            '<w:insideV w:val="none" w:sz="0" w:space="0" w:color="auto"/>'
            '</w:tblBorders>'
        )

        body_parts = []
        is_oficio = 'OFI' in (self.correspondence_type.prefix or '').upper() or 'OFICIO' in (self.correspondence_type.name or '').upper()
        is_memo = 'MEMO' in (self.correspondence_type.prefix or '').upper() or 'MEMORANDO' in (self.correspondence_type.name or '').upper()

        # 1. Encabezado de Documento (Oficio vs Memorando/Circular/Informe)
        if is_oficio:
            # Correlativo (izquierda) y Fecha (derecha) en la MISMA fila horizontal, idéntico al PDF
            corr_date_tbl = (
                f'<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/>{no_borders_xml}</w:tblPr>'
                '<w:tr>'
                f'<w:tc><w:tcPr><w:tcW w:w="2500" w:type="pct"/></w:tcPr>'
                f'{make_p(self.correlative or "", bold=True, color="002060", sz=26, align="left", after=0)}</w:tc>'
                f'<w:tc><w:tcPr><w:tcW w:w="2500" w:type="pct"/></w:tcPr>'
                f'{make_p(self.date_formatted_spanish_long or "", bold=True, color="000000", sz=22, align="right", after=0)}</w:tc>'
                '</w:tr></w:tbl>'
                '<w:p><w:pPr><w:spacing w:before="0" w:after="280"/></w:pPr></w:p>'
            )
            body_parts.append(corr_date_tbl)

            # Vocativo y Destinatarios en Azul Institucional (#002060) y Mayúsculas
            body_parts.append(make_p(self.get_recipient_treatment_label(), bold=True, color="000000", sz=22, align="left", after=40))
            recs = self.get_formatted_recipients_list()
            for idx, rec in enumerate(recs):
                is_last = (idx == len(recs) - 1)
                if rec.get('name'):
                    body_parts.append(make_p(rec['name'], bold=True, color="002060", sz=22, align="left", after=20))
                if rec.get('job'):
                    body_parts.append(make_p(rec['job'], bold=True, color="002060", sz=22, align="left", after=20))
                if rec.get('institution'):
                    body_parts.append(make_p(rec['institution'], bold=True, color="002060", sz=22, align="left", after=(40 if is_last else 100)))
            body_parts.append(make_p(self.get_recipient_closing_label(), bold=True, color="000000", sz=22, align="left", before=20, after=280))
        else:
            body_parts.append(make_p(self.correlative or '', bold=True, color="002060", sz=26, align="left", after=60))
            doc_title = (self.correspondence_type.name or 'MEMORANDO').upper()
            body_parts.append(make_p(doc_title, bold=True, underline=True, color="002060", sz=26, align="center", after=180))

            mgr = self.send_department_id.manager_id
            de_name = (mgr.name if mgr else '').strip().upper()
            de_job = (mgr.job_id.name if (mgr and mgr.job_id) else (self.send_department_id.name or '')).strip().upper()
            de_cell_p = []
            if de_name:
                de_cell_p.append(make_p(de_name, bold=True, color="000000", sz=22, align="left", after=10))
            de_cell_p.append(make_p(de_job, bold=True, color="000000", sz=22, align="left", after=80))

            para_cell_p = []
            recs = self.get_formatted_recipients_list()
            if recs:
                for rec in recs:
                    if rec.get('name'):
                        para_cell_p.append(make_p(rec['name'], bold=True, color="000000", sz=22, align="left", after=10))
                    if rec.get('job'):
                        para_cell_p.append(make_p(rec['job'], bold=True, color="000000", sz=22, align="left", after=60))
            else:
                para_cell_p.append(make_p("TODO EL PERSONAL", bold=True, color="000000", sz=22, align="left", after=80))

            def make_meta_row(label_txt, right_paragraphs_xml):
                return (
                    '<w:tr>'
                    f'<w:tc><w:tcPr><w:tcW w:w="750" w:type="pct"/></w:tcPr>'
                    f'{make_p(label_txt, bold=True, color="000000", sz=22, align="left", after=80)}</w:tc>'
                    f'<w:tc><w:tcPr><w:tcW w:w="4250" w:type="pct"/></w:tcPr>'
                    f'{"".join(right_paragraphs_xml)}</w:tc>'
                    '</w:tr>'
                )

            rows_meta = []
            if is_memo:
                rows_meta.append(make_meta_row("DE:", de_cell_p))
                rows_meta.append(make_meta_row("PARA:", para_cell_p))
            else:
                rows_meta.append(make_meta_row("PARA:", para_cell_p))
                rows_meta.append(make_meta_row("DE:", de_cell_p))
            rows_meta.append(make_meta_row("ASUNTO:", [make_p((self.name or '').upper(), bold=True, color="000000", sz=22, align="left", after=80)]))
            rows_meta.append(make_meta_row("FECHA:", [make_p(self.date_formatted_spanish or '', bold=True, color="000000", sz=22, align="left", after=80)]))

            meta_tbl = (
                f'<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/>{no_borders_xml}</w:tblPr>'
                f'{"".join(rows_meta)}</w:tbl>'
                '<w:p><w:pPr><w:spacing w:before="0" w:after="200"/></w:pPr></w:p>'
            )
            body_parts.append(meta_tbl)

        # 2. Cuerpo del Documento (con Saludo y Despedida Fijos)
        body_parts.append(self._html_to_ooxml_elements(self.get_full_body_html()))

        # 3. Bloque de Firma y Sello (con Imagen combinada de Sello + Firma igual que en el PDF)
        sig_png = self._build_signature_png_bytes()
        if sig_png:
            sig_drawing = make_drawing_xml("rIdSigImg1", 103, "SignatureStamp", 4389120, 1178560)
            body_parts.append(
                f'<w:p><w:pPr><w:spacing w:before="200" w:after="40"/><w:jc w:val="center"/></w:pPr>'
                f'<w:r>{sig_drawing}</w:r></w:p>'
            )
            before_signer = 0
        else:
            before_signer = 480

        mgr = self.send_department_id.manager_id
        signer_name = (mgr.name if mgr else (self.author_id.name if not is_oficio else "ERIC RICARDO MARTÍNEZ GÓMEZ")).upper()
        signer_job = (mgr.job_id.name if (mgr and mgr.job_id) else (self.send_department_id.name if not is_oficio else "DIRECTOR EJECUTIVO")).upper()
        body_parts.append(make_p(signer_name, bold=True, color="002060", sz=(22 if is_oficio else 24), align="center", before=before_signer, after=20))
        body_parts.append(make_p(signer_job, bold=True, color="002060", sz=20, align="center", after=20))
        body_parts.append(make_p("CENTRO NACIONAL DE INVESTIGACIÓN DE PESCA Y ACUICULTURA (CENIPA)", bold=True, color="002060", sz=20, align="center", after=60))
        if is_oficio:
            gaceta_txt = (
                "Decreto N° 4.807, publicado en la Gaceta Oficial de la República Bolivariana de Venezuela N° 42.634 "
                "de fecha 23 de mayo de 2023. Delegación atribuida en el artículo 1, numeral 2 Resolución DM/N° 005-24 "
                "publicada en la Gaceta Oficial de la República Bolivariana de Venezuela N° 42.942 de fecha 15 de agosto de 2.024"
            )
            body_parts.append(make_p(gaceta_txt, bold=False, color="000000", sz=16, align="center", after=160, left_ind=900, right_ind=900))
        elif mgr and mgr.director_description:
            desc_plain = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', unescape(str(mgr.director_description)))).strip()
            if desc_plain:
                body_parts.append(make_p(desc_plain, bold=False, color="000000", sz=16, align="center", after=160, left_ind=900, right_ind=900))

        # 4. Bloque Con Copia (C.c.)
        cc_items = self.get_formatted_cc_list()
        if cc_items:
            body_parts.append(make_p("C.c.:", bold=True, color="000000", sz=18, align="left", before=120, after=20))
            for cc in cc_items:
                cc_line = " — ".join([x for x in (cc.get('name'), cc.get('job')) if x])
                if cc.get('institution'):
                    cc_line += f" ({cc['institution']})"
                body_parts.append(make_p(cc_line, bold=True, color="002060", sz=18, align="left", after=20))

        # 5. Iniciales
        body_parts.append(make_p(self.author_initials or '', italic=True, color="000000", sz=18, align="left", before=160, after=120))

        # 6. Pestaña de Anexos (Salto de página posterior a la firma)
        if self.tiene_anexos and self.anexos_html:
            body_parts.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
            anexo_title = f"ANEXO AL DOCUMENTO: {self.correlative or ''}"
            anexo_hdr_tbl = (
                f'<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/>{no_borders_xml}</w:tblPr>'
                '<w:tr>'
                f'<w:tc><w:tcPr><w:tcW w:w="3250" w:type="pct"/></w:tcPr>'
                f'{make_p(anexo_title, bold=True, color="002060", sz=24, align="left", after=0)}</w:tc>'
                f'<w:tc><w:tcPr><w:tcW w:w="1750" w:type="pct"/></w:tcPr>'
                f'{make_p(self.date_formatted_spanish or "", bold=True, color="000000", sz=20, align="right", after=0)}</w:tc>'
                '</w:tr></w:tbl>'
                '<w:p><w:pPr><w:spacing w:before="0" w:after="200"/></w:pPr></w:p>'
            )
            body_parts.append(anexo_hdr_tbl)
            body_parts.append(self._html_to_ooxml_elements(self.anexos_html))

        header_png = self._build_header_png_bytes()
        footer_png = self._build_footer_png_bytes()

        # Header a 10mm de cada borde lateral (ancho 195.9mm = 7050060 EMUs, sangría -873 twips)
        header_drawing = make_drawing_xml("rIdHdrImg1", 101, "HeaderBanner", 7050060, 734380)
        header_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<w:p><w:pPr><w:spacing w:before="0" w:after="120"/><w:ind w:left="-873" w:right="-873"/><w:jc w:val="center"/></w:pPr><w:r>{header_drawing}</w:r></w:p>'
            '</w:hdr>'
        )

        # Footer sangrado 100% de borde a borde (ancho total de página 215.9mm = 7772400 EMUs, sangría -1440 twips, w:footer="0")
        footer_drawing = make_drawing_xml("rIdFtrImg1", 102, "FooterBanner", 7772400, 1639490)
        footer_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<w:p><w:pPr><w:spacing w:before="0" w:after="0"/><w:ind w:left="-1440" w:right="-1440"/><w:jc w:val="center"/></w:pPr><w:r>{footer_drawing}</w:r></w:p>'
            '</w:ftr>'
        )

        page_h = "20160" if is_oficio else "15840"  # Legal (Oficio) vs Letter (Carta) en twips
        sect_pr = (
            f'<w:sectPr>'
            '<w:headerReference w:type="default" r:id="rIdHeader1"/>'
            '<w:footerReference w:type="default" r:id="rIdFooter1"/>'
            f'<w:pgSz w:w="12240" w:h="{page_h}"/>'
            '<w:pgMar w:top="1758" w:right="1440" w:bottom="2608" w:left="1440" w:header="340" w:footer="0" w:gutter="0"/>'
            '</w:sectPr>'
        )

        document_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<w:body>{"".join(body_parts)}{sect_pr}</w:body></w:document>'
        )

        content_types_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>'
            '<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'
            '</Types>'
        )

        rels_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>'
        )

        sig_rel_entry = (
            '<Relationship Id="rIdSigImg1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/signature_banner.png"/>'
            if sig_png else ''
        )
        doc_rels_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rIdHeader1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>'
            '<Relationship Id="rIdFooter1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>'
            f'{sig_rel_entry}'
            '</Relationships>'
        )

        header_rels_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rIdHdrImg1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/header_banner.png"/>'
            '</Relationships>'
        )

        footer_rels_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rIdFtrImg1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/footer_banner.png"/>'
            '</Relationships>'
        )

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('[Content_Types].xml', content_types_xml)
            zf.writestr('_rels/.rels', rels_xml)
            zf.writestr('word/_rels/document.xml.rels', doc_rels_xml)
            zf.writestr('word/_rels/header1.xml.rels', header_rels_xml)
            zf.writestr('word/_rels/footer1.xml.rels', footer_rels_xml)
            zf.writestr('word/header1.xml', header_xml.encode('utf-8'))
            zf.writestr('word/footer1.xml', footer_xml.encode('utf-8'))
            zf.writestr('word/media/header_banner.png', header_png)
            zf.writestr('word/media/footer_banner.png', footer_png)
            if sig_png:
                zf.writestr('word/media/signature_banner.png', sig_png)
            zf.writestr('word/document.xml', document_xml.encode('utf-8'))
        return buf.getvalue()

    def action_download_docx(self):
        """
        Genera y descarga el documento en formato editable Word (.docx)
        manteniendo la estructura oficial de Correspondencia CENIPA.
        """
        self.ensure_one()
        docx_bytes = self._build_docx_bytes()
        clean_corr = (self.correlative or 'Documento').replace('/', '-')
        fname = f"{clean_corr}.docx"
        self.write({
            'docx_file': base64.b64encode(docx_bytes),
            'docx_filename': fname,
        })
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{self._name}/{self.id}/docx_file/{fname}?download=true',
            'target': 'self',
        }

    def action_send(self):
        self.write({'state': 'sent'})

    def action_assign(self):
        """Acción para correspondencia entrante. Cambia el estado a 'Asignado'."""
        self.ensure_one()
        if self.correspondence_flow != 'incoming':
            raise UserError(_("Esta acción solo es válida para correspondencia entrante."))
        self.write({'state': 'assigned'})

    def action_dispatch(self):
        """Acción para correspondencia saliente. Cambia el estado a 'Despachado'."""
        self.ensure_one()
        self.write({'state': 'dispatched'})

    def action_read(self):
        self.ensure_one()
        user_department = self.env.user.employee_id.department_id

        # Validación de seguridad: Solo los usuarios de un departamento destinatario pueden marcar como leído.
        if not user_department or user_department not in self.recipient_department_ids:
            raise models.ValidationError(_("Solo los usuarios de un departamento destinatario pueden marcar este documento como leído."))

        self.env['correspondence.document.read_status'].create({
            'document_id': self.id,
            'department_id': user_department.id,
            'read_by_user_id': self.env.user.id,
        })

        # Devolver una acción para recargar la vista y reevaluar los attrs de los botones.
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }
    
    def action_generate_report(self):
        self.ensure_one()
        # Usar la acción de reporte definida en el tipo de correspondencia.
        # Si no hay ninguna, usar una acción de fallback genérica.
        report_action = self.correspondence_type.report_action_id
        if not report_action:
            # Usamos la acción genérica como fallback
            report_action = self.env.ref('correspondence.action_report_correspondence_document')

        return report_action.report_action(self)

    def action_reply(self):
        self.ensure_one()
        # Prepara el contexto base con valores por defecto para la respuesta
        ctx = {
            'default_parent_document_id': self.id,
            'default_name': f"Re: {self.correlative} - {self.name}",
            'default_correspondence_type': self.correspondence_type.id,
        }

        # Si estamos respondiendo a un documento ENTRANTE, la respuesta debe ser SALIENTE
        if self.correspondence_flow == 'incoming':
            ctx['default_correspondence_flow'] = 'outgoing'
            ctx['default_external_partner_id'] = self.external_partner_id.id
        # Si estamos respondiendo a un documento INTERNO, la respuesta sigue siendo INTERNA
        else:
            ctx['default_correspondence_flow'] = 'internal'
            ctx['default_recipient_department_ids'] = [(6, 0, self.send_department_id.ids)]

        return {
            'name': 'Responder Correspondencia',
            'type': 'ir.actions.act_window',
            'res_model': 'correspondence_document',
            'view_mode': 'form',
            'target': 'current',  # Abrir en la ventana principal
            'context': ctx,
        }

    @api.model
    def create(self, vals):
        # Determinar el estado inicial basado en el flujo
        if vals.get('correspondence_flow') == 'incoming':
            vals['state'] = 'received'
        else:
            vals.setdefault('state', 'draft')

        # Asignar correlativo temporal
        if 'correlative' not in vals:
            vals['correlative'] = _('Nuevo')

        new_document = super(correspondence_document, self).create(vals)

        # Generar correlativo final
        if new_document.correlative == _('Nuevo'):
            new_correlative = ''
            if new_document.correspondence_flow == 'incoming':
                # Usar una secuencia global para correspondencia entrante
                sequence = self.env['ir.sequence'].next_by_code('correspondence.incoming')
                new_correlative = f"ENT-{sequence}" if sequence else _('Error de Secuencia')

            elif new_document.correspondence_flow in ['internal', 'outgoing']:
                # Usar la lógica existente para interna y saliente
                department = new_document.send_department_id
                corr_type = new_document.correspondence_type

                if department and corr_type:
                    correlative_obj = self.env['correspondence.department.correlative'].search([
                        ('department_id', '=', department.id),
                        ('correspondence_type_id', '=', corr_type.id)
                    ], limit=1)

                    if not correlative_obj:
                        correlative_obj = self.env['correspondence.department.correlative'].create({
                            'department_id': department.id,
                            'correspondence_type_id': corr_type.id,
                            'last_sequence': 0
                        })

                    new_sequence = correlative_obj.last_sequence + 1
                    correlative_obj.last_sequence = new_sequence
                    doc_date = new_document.date or fields.Date.today()
                    year = doc_date.strftime('%Y')
                    new_correlative = f"{department.correlative_prefix or 'S-PRE'}-{corr_type.prefix or ''}-{year}-{str(new_sequence).zfill(4)}"

            if new_correlative:
                new_document.write({'correlative': new_correlative})

        # Si este nuevo documento es una respuesta a otro (tiene un padre)
        if new_document.parent_document_id:
            # Cambiamos el estado del documento padre a 'Respondido'
            new_document.parent_document_id.write({'state': 'replied'})

        if new_document.external_partner_ids and not new_document.external_partner_id:
            new_document.write({'external_partner_id': new_document.external_partner_ids[0].id})
        elif new_document.external_partner_id and not new_document.external_partner_ids:
            new_document.write({'external_partner_ids': [(4, new_document.external_partner_id.id)]})

        return new_document

    def unlink(self):
        """
        Sobrescribe el método de eliminación para impedirla en todos los casos.
        Los documentos de correspondencia no se pueden eliminar para mantener la integridad de los correlativos y el historial.
        """
        raise UserError(_('Los documentos de correspondencia no pueden ser eliminados. Si es necesario, considere archivar o cancelar el documento en lugar de borrarlo.'))

    document_file = fields.Binary(string='Archivo', attachment=True, copy=False, help="El documento firmado y sellado.")
    file_name = fields.Char(string="Nombre de Archivo")

    extra_attachment_ids = fields.Many2many(
        comodel_name='ir.attachment',
        relation='correspondence_document_extra_attachment_rel',
        column1='document_id',
        column2='attachment_id',
        string="Archivos Adjuntos Adicionales"
    )

    def action_open_signed_document(self):
        """
        Genera una acción para descargar el documento firmado.
        Si no existe, abre el formulario como fallback.
        """
        self.ensure_one()
        if self.document_file:
            return {
                'type': 'ir.actions.act_url',
                'url': f'/web/content/{self._name}/{self.id}/document_file/{self.file_name}?download=true',
                'target': 'new',
            }
        # Si no hay archivo, informamos al usuario en lugar de abrir el formulario.
        raise UserError(_("Este documento no tiene un archivo firmado adjunto para descargar."))

    def name_get(self):
        result = []
        for doc in self:
            name = f"{doc.correlative or 'Nuevo'} - {doc.name}"
            result.append((doc.id, name))
        return result

    def get_state_display_name(self):
        """Método auxiliar para obtener el nombre legible de un campo de selección."""
        self.ensure_one()
        # Obtiene la lista de tuplas (valor, etiqueta) del campo 'state'
        selection_list = self._fields['state'].selection
        # Devuelve la etiqueta que corresponde al valor actual del estado
        return dict(selection_list).get(self.state)

    parent_document_id = fields.Many2one('correspondence_document', string='En respuesta a')
    child_document_ids = fields.One2many('correspondence_document', 'parent_document_id', string='Respuestas')

    parent_author_id = fields.Many2one(
        'res.users',
        string="Respuesta a",
        related='parent_document_id.author_id',
        store=True)

    @api.depends('access_token')
    def _compute_public_url(self):
        """Genera la URL pública para el documento."""
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        for doc in self:
            if doc.id and doc.access_token:
                doc.public_url = f"{base_url}/correspondence/public/{doc.id}?access_token={doc.access_token}"
            else:
                doc.public_url = False

    def get_qr_code_url(self):
        """Devuelve una Data URI Base64 del código QR para renderizado directo en PDF/QWeb sin peticiones HTTP externas."""
        self.ensure_one()
        if not self.access_token:
            self.access_token = ''.join(secrets.token_urlsafe(32))
        
        url = self.public_url
        if not url:
            base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
            if self.id and self.access_token:
                url = f"{base_url}/correspondence/public/{self.id}?access_token={self.access_token}"
                self.public_url = url
            
        if url:
            try:
                barcode_bytes = self.env['ir.actions.report'].barcode('QR', url, width=250, height=250)
                if barcode_bytes:
                    b64_str = base64.b64encode(barcode_bytes).decode('utf-8')
                    return f"data:image/png;base64,{b64_str}"
            except Exception:
                pass
        return False

    @api.model
    def action_open_outbox(self):
        """
        Esta función es llamada por el menú 'Bandeja de Salida'.
        Construye y devuelve una acción de ventana con un dominio dinámico
        que filtra los documentos por el departamento del usuario actual.
        """
        user_department_id = self.env.user.employee_id.department_id.id
        if not user_department_id:
            raise UserError(_("Tu usuario no tiene un departamento asignado. Por favor, contacta a un administrador."))

        action = self.env['ir.actions.act_window']._for_xml_id('correspondence.action_correspondence_document_tree')
        action['display_name'] = _('Bandeja de Salida')
        action['name'] = _('Bandeja de Salida')
        action['domain'] = [('send_department_id', '=', user_department_id)]
        action['context'] = {} # Limpiamos el contexto para evitar filtros no deseados
        return action

    @api.model
    def action_open_archive(self):
        """
        Esta función es llamada por el menú 'Archivo'.
        Construye y devuelve una acción de ventana con un dominio dinámico
        que filtra los documentos enviados o recibidos por el departamento del usuario.
        """
        user_department_id = self.env.user.employee_id.department_id.id
        if not user_department_id:
            raise UserError(_("Tu usuario no tiene un departamento asignado. Por favor, contacta a un administrador."))

        action = self.env['ir.actions.act_window']._for_xml_id('correspondence.action_correspondence_document_archive_base')
        
        # Construimos el dominio para mostrar documentos enviados O recibidos por el departamento del usuario
        domain = [
            '|',
                ('send_department_id', '=', user_department_id),
                ('recipient_department_ids', 'in', [user_department_id])
        ]
        action['domain'] = domain
        return action
