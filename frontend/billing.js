(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let sessionEpoch = 0;
  const api = async (...args) => { const epoch = sessionEpoch; const result = await window.agentPrivateApi(...args); if (epoch !== sessionEpoch) throw new Error('La sesión se cerró. Vuelve a iniciar sesión.'); return result; };
  let config = null, working = false;
  const node = (tag, value, className) => { const element = document.createElement(tag); if (value != null) element.textContent = value; if (className) element.className = className; return element; };
  const post = (path, body = {}) => api('/api/billing' + path, { method: 'POST', body: JSON.stringify(body) });
  const remove = path => api('/api/billing' + path, { method: 'DELETE', body: JSON.stringify({ confirmed: true }) });
  const status = value => String(value || '').replaceAll('_', ' ').toLowerCase();
  function fields(values) { const list = node('dl', null, 'billing-fields'); for (const [name, value] of values) list.append(node('dt', name), node('dd', value || 'No legible / no disponible')); return list; }
  function button(label, action, style = 'secondary') { const item = node('button', label, style); item.type = 'button'; item.addEventListener('click', () => run(action)); return item; }
  async function run(action) {
    if (working) return;
    working = true; $('billing-error').hidden = true; $('billing-message').textContent = 'Procesando…';
    try { const result = await action(); $('billing-message').textContent = typeof result === 'string' ? result : 'Datos actualizados. Revisa el resultado antes de continuar.'; }
    catch (error) { $('billing-error').textContent = error.message; $('billing-error').hidden = false; $('billing-message').textContent = ''; }
    finally { working = false; }
  }
  async function refresh() {
    config = await api('/api/billing/config');
    $('billing-disabled').hidden = config.enabled; $('billing-content').hidden = !config.enabled;
    $('billing-key-setup').hidden = config.enabled || config.has_encrypted_records;
    if (!config.enabled && config.has_encrypted_records) $('billing-disabled').textContent = 'Este servidor ya tiene información cifrada. Recupera su clave original; no generes otra porque no permitiría abrir los datos anteriores.';
    if (!config.enabled) return;
    const profiles = $('fiscal-profiles'); profiles.replaceChildren();
    for (const id of ['csf-replace', 'ticket-profile']) { const select = $(id), selected = select.value; select.replaceChildren(new Option(id === 'csf-replace' ? 'Agregar otro perfil' : 'Selecciona un perfil', '')); for (const profile of config.profiles) select.add(new Option(profile.name + ' · ' + profile.rfc, profile.id)); select.value = selected; }
    $('analyze-ticket').disabled = config.profiles.length === 0;
    if (!config.profiles.length) profiles.append(node('p', 'Aún no hay perfil confirmado. Envía tu constancia para comenzar.'));
    for (const profile of config.profiles) {
      const item = node('div', null, 'billing-record'); item.append(node('strong', profile.name), node('span', profile.rfc + ' · CP ' + profile.postal_code), node('span', 'Régimen ' + profile.fiscal_regime + ' · Uso ' + profile.cfdi_use + ' · ' + profile.email));
      item.append(button('Eliminar perfil y archivos', async () => { if (!confirm('¿Eliminar este perfil, sus solicitudes y archivos asociados?')) return; await remove('/profiles/' + profile.id); clearDetails(); await refresh(); })); profiles.append(item);
    }
    $('gmail-status').textContent = config.gmail_connected ? 'Buzón conectado: ' + config.gmail_email : config.gmail_configured ? 'Gmail está listo para que autorices tu buzón.' : 'La conexión de Google todavía requiere configuración en el servidor.';
    $('connect-gmail').disabled = !config.gmail_configured; $('disconnect-gmail').hidden = !config.gmail_connected;
    const list = $('billing-tickets'); list.replaceChildren();
    if (!config.tickets.length) list.append(node('p', 'Aquí aparecerán los tickets y su seguimiento.'));
    for (const ticket of config.tickets) { const item = node('div', null, 'billing-record'); item.append(node('strong', ticket.merchant || 'Ticket'), node('span', 'Folio ' + (ticket.folio || 'ilegible') + ' · ' + (ticket.total || 'Importe ilegible') + ' · ' + status(ticket.status)), button('Revisar solicitud', () => showTicket(ticket.id))); list.append(item); }
    const documents = $('billing-documents'); documents.replaceChildren();
    for (const document of config.documents || []) { const row = node('div', null, 'billing-record'); row.append(node('span', ({ csf: 'Constancia', ticket: 'Ticket', invoice_pdf: 'Factura PDF', invoice_xml: 'Factura XML' }[document.purpose] || 'Documento') + ' · Vence: ' + new Date(document.expires * 1000).toLocaleString('es-MX')), button('Eliminar archivo', async () => { if (!confirm('¿Eliminar este archivo original? Los envíos que lo necesiten se bloquearán.')) return; await remove('/documents/' + document.id); await refresh(); })); documents.append(row); }
  }
  function clearDetails() { for (const id of ['fiscal-draft', 'billing-ticket-detail', 'billing-email-preview']) { $(id).replaceChildren(); $(id).hidden = true; } }
  async function upload(formId, purpose) {
    const prefix = purpose === 'csf' ? 'csf' : 'ticket', file = $(prefix + '-file').files[0];
    if (!file || !$(prefix + '-consent').checked) throw new Error('Selecciona el documento y autoriza expresamente su análisis.');
    if (file.size > config.max_upload_bytes) throw new Error('El documento supera el tamaño permitido.');
    const profile = purpose === 'csf' ? $('csf-replace').value : $('ticket-profile').value;
    if (purpose === 'ticket' && !profile) throw new Error('Primero confirma y selecciona un perfil fiscal.');
    const query = new URLSearchParams({ purpose }); if (profile) query.set('profile_id', profile);
    const document = await api('/api/billing/documents?' + query, { method: 'POST', body: file, headers: { 'Content-Type': 'application/octet-stream' } });
    const result = await post('/documents/' + document.id + '/analyze', { consent_to_ai: true, replace_profile_id: purpose === 'csf' && profile ? profile : null });
    $(formId).reset();
    if (result.type === 'fiscal_draft') showDraft(result.draft); else { await refresh(); await showTicket(result.ticket.id); }
  }
  function showDraft(draft) {
    const container = $('fiscal-draft'); container.replaceChildren(); container.hidden = false;
    container.append(node('h3', 'Revisa los datos extraídos'), fields([['RFC', draft.rfc], ['Nombre o razón social', draft.name], ['Código postal fiscal', draft.postal_code], ['Régimen fiscal', draft.fiscal_regime]]));
    const form = node('form', null, 'billing-form'), use = node('select'); use.id = 'draft-cfdi-use'; use.required = true; use.add(new Option('Elige el uso de CFDI', ''));
    for (const [code, label] of Object.entries(config.cfdi_uses)) use.add(new Option(code + ' · ' + label, code));
    const email = node('input'); email.id = 'draft-email'; email.type = 'email'; email.required = true; email.maxLength = 254;
    const useLabel = node('label', 'Uso de CFDI elegido por ti'); useLabel.htmlFor = use.id;
    const emailLabel = node('label', 'Correo autorizado para recibir las facturas'); emailLabel.htmlFor = email.id;
    const submit = node('button', 'Confirmar y guardar perfil', 'primary'); submit.type = 'submit'; submit.disabled = !draft.complete;
    form.append(useLabel, use, emailLabel, email, submit);
    form.addEventListener('submit', event => { event.preventDefault(); run(async () => { await post('/profiles/drafts/' + draft.id + '/confirm', { cfdi_use: use.value, email: email.value, confirmed: true }); container.replaceChildren(); container.hidden = true; await refresh(); }); });
    container.append(form); if (!draft.complete) container.append(node('p', 'Faltan datos fiscales legibles. Envía una copia más clara; no completaremos campos por suposición.'));
  }
  async function showTicket(identifier) {
    const ticket = await api('/api/billing/tickets/' + identifier), panel = $('billing-ticket-detail'); panel.replaceChildren(); panel.hidden = false;
    $('billing-email-preview').hidden = true;
    panel.append(node('h3', ticket.merchant || 'Ticket'), fields([['Estado', status(ticket.status)], ['RFC del emisor', ticket.issuer_rfc], ['Fecha', ticket.purchase_date], ['Folio', ticket.folio], ['Total', ticket.total], ['Sucursal', ticket.branch], ['Código de facturación', ticket.billing_code], ['Portal detectado', ticket.billing_url], ['Correo detectado', ticket.billing_email]]), node('p', ticket.channels.reason), node('p', ticket.note));
    if (ticket.channels.official_source) { const official = node('a', 'Revisar fuente oficial del destinatario', 'billing-download'); official.href = ticket.channels.official_source; official.target = '_blank'; official.rel = 'noopener noreferrer'; panel.append(official); }
    const source = node('a', 'Ver ticket original', 'billing-download'); source.href = '/api/billing/documents/' + ticket.document_id; panel.append(source);
    if (ticket.billing_email && ticket.email_explicitly_for_billing && !ticket.verified_email) panel.append(button('Confirmo el destinatario para facturación', async () => { if (!confirm('¿Revisaste el ticket original o la fuente oficial indicada y este correo está destinado expresamente a facturación?')) return; await post('/tickets/' + identifier + '/verify-email', { confirmed: true }); await showTicket(identifier); }));
    if (ticket.verified_email && !ticket.delivery) panel.append(button('Preparar correo para revisión', async () => showEmail(await post('/tickets/' + identifier + '/prepare-email'))));
    panel.append(button('El comercio solicitó expresamente mi constancia', async () => { const evidence = prompt('Describe la petición expresa que recibiste del comercio:'); if (!evidence) return; await post('/tickets/' + identifier + '/certificate-required', { confirmed: true, evidence }); await showTicket(identifier); }));
    if (ticket.certificate_requested && ticket.verified_email) panel.append(button('Autorizar adjuntar CSF y revisar correo', async () => { if (!confirm('¿Autorizas adjuntar tu constancia completa a este comercio para esta solicitud?')) return; showEmail(await post('/tickets/' + identifier + '/prepare-email', { include_csf: true, authorize_csf: true })); }));
    if (ticket.approval_id) panel.append(button(ticket.delivery ? 'Ver solicitud enviada' : 'Revisar aprobación existente', async () => showEmail(await api('/api/billing/email-approvals/' + ticket.approval_id))));
    if (ticket.delivery) panel.append(button('Consultar respuestas en Gmail', async () => { await post('/tickets/' + identifier + '/track', { authorize_sat: false }); await refresh(); await showTicket(identifier); }));
    if (ticket.delivery && config.background_tracking_enabled) panel.append(button(ticket.tracking?.enabled ? 'Detener seguimiento automático' : 'Activar seguimiento automático', async () => { const enabled = !ticket.tracking?.enabled; if (enabled && !confirm(`Se consultará Gmail cada ${config.tracking_interval_seconds / 60} minutos, hasta ${config.tracking_max_checks} consultas por ticket y con un máximo de tres errores consecutivos. No enviará recordatorios ni consultará el SAT. ¿Autorizas este seguimiento?`)) return; await post('/tickets/' + identifier + '/tracking', { enabled, confirmed: true }); await showTicket(identifier); }));
    if (ticket.validation) panel.append(button('Autorizar consulta de vigencia al SAT', async () => { if (!confirm('Se compartirán con el SAT el RFC del emisor y receptor, UUID e importe. No se envía la constancia. ¿Autorizas esta consulta?')) return; await post('/tickets/' + identifier + '/validate', { authorize_sat: true }); await refresh(); await showTicket(identifier); }));
    for (const documentId of ticket.invoice_documents || []) { const link = node('a', 'Descargar archivo de factura', 'billing-download'); link.href = '/api/billing/documents/' + documentId; panel.append(link); }
    panel.append(button('Eliminar solicitud y archivos', async () => { if (!confirm('¿Eliminar la solicitud y sus archivos? Antes comprueba cualquier envío cuyo resultado sea incierto.')) return; await remove('/tickets/' + identifier); clearDetails(); await refresh(); }));
    for (const reply of (ticket.responses || []).slice(0, 3)) { const details = node('details', null, 'billing-response'); details.append(node('summary', 'Respuesta del comercio · Solo información, no autoriza acciones'), node('pre', reply.text || 'Respuesta sin texto.', 'billing-email-body')); panel.append(details); }
  }
  function showEmail(approval) {
    const panel = $('billing-email-preview'); panel.replaceChildren(); panel.hidden = false;
    panel.append(node('h3', 'Confirma únicamente este envío'), fields([['Destinatario', approval.to], ['Asunto', approval.subject], ...Object.entries(approval.fiscal_data).map(([key, value]) => [{ rfc: 'RFC', name: 'Nombre', postal_code: 'CP fiscal', fiscal_regime: 'Régimen', cfdi_use: 'Uso CFDI', email: 'Correo de recepción' }[key], value]), ['Adjuntos', approval.attachments.map(item => item.label).join(', ')]]), node('pre', approval.body, 'billing-email-body'));
    if (!config.email_send_enabled || !config.gmail_connected) panel.append(node('p', 'El envío real está desactivado o falta conectar Gmail. Puedes revisar este borrador; no se enviará automáticamente.'));
    const send = button('Confirmar y enviar este correo', async () => { await post('/email-approvals/' + approval.id + '/send', { confirmed: true }); panel.hidden = true; await refresh(); await showTicket(approval.ticket_id); }, 'primary'); send.disabled = !config.email_send_enabled || !config.gmail_connected || (approval.state && approval.state !== 'pending');
    panel.append(send, button('Rechazar envío', async () => { await post('/email-approvals/' + approval.id + '/reject'); panel.hidden = true; await refresh(); await showTicket(approval.ticket_id); }));
    panel.scrollIntoView({ block: 'start', behavior: 'smooth' });
  }
  $('open-billing').addEventListener('click', () => { $('billing-dialog').showModal(); run(refresh); });
  $('close-billing').addEventListener('click', () => $('billing-dialog').close());
  $('refresh-billing').addEventListener('click', () => run(refresh));
  $('csf-form').addEventListener('submit', event => { event.preventDefault(); run(() => upload('csf-form', 'csf')); });
  $('ticket-form').addEventListener('submit', event => { event.preventDefault(); run(() => upload('ticket-form', 'ticket')); });
  $('connect-gmail').addEventListener('click', () => run(async () => { const result = await post('/gmail/connect'); const url = new URL(result.authorization_url); if (url.protocol !== 'https:' || url.hostname !== 'accounts.google.com') throw new Error('Destino de autorización no permitido.'); window.location.assign(url.href); }));
  $('disconnect-gmail').addEventListener('click', () => run(async () => { if (!confirm('¿Desconectar este buzón del agente? Los permisos de Google también pueden revocarse desde tu cuenta de Google.')) return; await post('/gmail/disconnect', { confirmed: true }); await refresh(); }));
  $('generate-billing-key').addEventListener('click', () => { if (!config || config.enabled || config.has_encrypted_records) return; const bytes = crypto.getRandomValues(new Uint8Array(32)); $('generated-billing-key').value = btoa(String.fromCharCode(...bytes)).replaceAll('+', '-').replaceAll('/', '_'); });
  $('copy-billing-key').addEventListener('click', () => run(async () => { const value = $('generated-billing-key').value; if (!value) throw new Error('Primero genera una clave nueva.'); await navigator.clipboard.writeText(value); return 'Clave nueva copiada. Guárdala en BILLING_ENCRYPTION_KEY del servidor y conserva una copia segura fuera del chat.'; }));
  $('billing-dialog').addEventListener('close', () => { $('generated-billing-key').value = ''; });
  document.addEventListener('agent:signedout', () => { sessionEpoch++; $('billing-dialog').close(); clearDetails(); config = null; for (const id of ['fiscal-profiles', 'billing-tickets', 'gmail-status', 'billing-documents']) $(id).replaceChildren(); for (const id of ['csf-form', 'ticket-form']) $(id).reset(); for (const id of ['csf-replace', 'ticket-profile']) $(id).replaceChildren(new Option('Selecciona un perfil', '')); });
})();
