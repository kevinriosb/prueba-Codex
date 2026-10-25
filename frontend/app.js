(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let csrf = '', current = null, configuration = null, busy = false;
  const toolNames = { calculator: 'Cálculo', current_server_time: 'Fecha y hora' };
  function text(tag, content, className) {
    const node = document.createElement(tag);
    node.textContent = content;
    if (className) node.className = className;
    return node;
  }
  async function api(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 100000);
    try {
      const response = await fetch(path, { ...options, credentials: 'same-origin', signal: controller.signal,
        headers: { 'Content-Type': 'application/json', ...(csrf ? { 'X-CSRF-Token': csrf } : {}), ...options.headers } });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        if (response.status === 401 && path !== '/api/auth/login') showLogin();
        throw new Error(body.error?.message || 'No se pudo completar la solicitud.');
      }
      return body;
    } catch (error) {
      if (error.name === 'AbortError' || error instanceof TypeError) throw new Error('Se perdió la conexión. Revisa el historial antes de repetir la instrucción.');
      throw error;
    } finally { clearTimeout(timer); }
  }
  window.agentPrivateApi = api;
  function showError(id, message) { $(id).textContent = message; $(id).hidden = !message; }
  function showLogin() { csrf = ''; current = null; $('workspace').hidden = true; $('login-view').hidden = false; $('messages').replaceChildren(); document.dispatchEvent(new Event('agent:signedout')); }
  function setBusy(value) {
    busy = value;
    for (const id of ['send-button', 'new-chat', 'delete-chat', 'logout', 'approve-action', 'reject-action']) $(id).disabled = value;
    $('message-input').disabled = value || Boolean(current?.approval);
    $('send-button').disabled = value || Boolean(current?.approval) || Boolean(current?.blocked);
    $('message-input').disabled = value || Boolean(current?.approval) || Boolean(current?.blocked);
    $('thinking').hidden = !value;
    $('message-form').setAttribute('aria-busy', String(value));
  }
  function closeMenu() { $('sidebar').classList.remove('open'); $('sidebar-shade').hidden = true; $('menu-button').setAttribute('aria-expanded', 'false'); }
  async function refreshConfig() {
    configuration = await api('/api/config');
    $('model-label').textContent = configuration.model;
    $('configuration-warning').hidden = configuration.ai_configured;
    $('message-input').maxLength = configuration.max_input_chars;
    $('usage-label').textContent = `${configuration.usage.runs} de ${configuration.max_daily_runs} ejecuciones hoy.`;
    updateCount();
  }
  function updateCount() { $('character-count').textContent = `${$('message-input').value.length} / ${configuration?.max_input_chars || 4000}`; }
  async function listConversations() {
    const data = await api('/api/conversations');
    const list = $('conversation-list');
    list.replaceChildren();
    if (!data.conversations.length) list.append(text('p', 'Tus conversaciones aparecerán aquí.', 'empty-history'));
    for (const item of data.conversations) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = item.id === current?.id ? 'active' : '';
      button.append(text('strong', 'Conversación'), text('span', new Date(item.created * 1000).toLocaleString('es-MX', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })));
      button.addEventListener('click', async () => { if (busy) return; try { await openConversation(item.id); closeMenu(); } catch (error) { showError('chat-error', error.message); } });
      list.append(button);
    }
    return data.conversations;
  }
  function renderConversation(data) {
    current = data;
    const messages = $('messages');
    messages.replaceChildren();
    $('welcome').hidden = data.messages.length > 0;
    for (const message of data.messages) {
      const wrapper = document.createElement('article');
      wrapper.className = `message ${['user', 'assistant', 'error'].includes(message.role) ? message.role : 'assistant'}`;
      wrapper.append(text('span', message.role === 'user' ? 'TÚ' : message.role === 'error' ? '!' : '✦', 'message-avatar'));
      const body = text('div', '', 'message-body');
      body.append(text('div', message.role === 'user' ? 'Tú' : message.role === 'error' ? 'Aviso' : 'Asistente', 'message-name'), text('div', message.content, 'message-text'));
      for (const event of message.events || []) {
        const details = document.createElement('details');
        details.className = 'tool-event';
        details.append(text('summary', `${toolNames[event.tool] || event.tool} · Ver resultado`), text('pre', event.result));
        body.append(details);
      }
      wrapper.append(body); messages.append(wrapper);
    }
    $('approval-panel').hidden = !data.approval;
    $('approval-details').replaceChildren();
    if (data.approval) {
      for (const tool of data.approval.tools) $('approval-details').append(text('strong', tool.name), text('pre', JSON.stringify(tool.arguments, null, 2)));
      $('approval-expiry').textContent = `Confirma únicamente si entiendes esta acción. Caduca a las ${new Date(data.approval.expires * 1000).toLocaleTimeString('es-MX', { hour: '2-digit', minute: '2-digit' })}.`;
    }
    $('delete-chat').disabled = busy;
    $('message-input').disabled = busy || Boolean(data.approval) || Boolean(data.blocked);
    $('send-button').disabled = busy || Boolean(data.approval) || Boolean(data.blocked);
    requestAnimationFrame(() => { $('chat-scroll').scrollTop = $('chat-scroll').scrollHeight; });
  }
  async function openConversation(id) { renderConversation(await api(`/api/conversations/${id}`)); await listConversations(); showError('chat-error', ''); }
  async function newConversation() { renderConversation(await api('/api/conversations', { method: 'POST' })); await listConversations(); closeMenu(); }
  async function enter(session) {
    csrf = session.csrf_token;
    $('profile-name').textContent = session.username;
    $('login-view').hidden = true; $('workspace').hidden = false;
    await refreshConfig();
    const conversations = await listConversations();
    if (conversations.length) await openConversation(conversations[0].id); else await newConversation();
  }
  $('login-form').addEventListener('submit', async event => {
    event.preventDefault(); $('login-button').disabled = true; showError('login-error', '');
    try { const session = await api('/api/auth/login', { method: 'POST', body: JSON.stringify({ username: $('username').value, password: $('password').value }) }); $('password').value = ''; await enter(session); }
    catch (error) { showLogin(); showError('login-error', error.message); }
    finally { $('password').value = ''; $('login-button').disabled = false; }
  });
  $('message-form').addEventListener('submit', async event => {
    event.preventDefault();
    const message = $('message-input').value.trim();
    if (!message || busy || !current || current.approval || current.blocked) return;
    if (!configuration?.ai_configured) { showError('chat-error', 'Configura AI_API_KEY en el servidor para activar las respuestas.'); return; }
    setBusy(true); showError('chat-error', '');
    try {
      const result = await api(`/api/conversations/${current.id}/messages`, { method: 'POST', body: JSON.stringify({ text: message }) });
      $('message-input').value = ''; updateCount(); renderConversation(result); await listConversations(); await refreshConfig();
    } catch (error) {
      if (current) { try { renderConversation(await api(`/api/conversations/${current.id}`)); } catch {} }
      showError('chat-error', error.message);
    } finally { setBusy(false); if (!current?.approval) $('message-input').focus(); }
  });
  async function decide(approved) {
    if (busy || !current?.approval) return;
    const approvalId = current.approval.id;
    setBusy(true); showError('chat-error', '');
    try { renderConversation(await api(`/api/conversations/${current.id}/approvals/${approvalId}`, { method: 'POST', body: JSON.stringify({ approved }) })); await refreshConfig(); }
    catch (error) { try { renderConversation(await api(`/api/conversations/${current.id}`)); } catch {} showError('chat-error', error.message); }
    finally { setBusy(false); }
  }
  $('approve-action').addEventListener('click', () => decide(true));
  $('reject-action').addEventListener('click', () => decide(false));
  $('new-chat').addEventListener('click', () => { if (!busy) newConversation().catch(error => showError('chat-error', error.message)); });
  $('delete-chat').addEventListener('click', async () => {
    if (busy || !current || !window.confirm('¿Eliminar esta conversación y sus mensajes? Esta acción no se puede deshacer.')) return;
    try { await api(`/api/conversations/${current.id}`, { method: 'DELETE', body: JSON.stringify({ confirmed: true }) }); current = null; const remaining = await listConversations(); if (remaining.length) await openConversation(remaining[0].id); else await newConversation(); }
    catch (error) { showError('chat-error', error.message); }
  });
  $('logout').addEventListener('click', async () => { try { await api('/api/auth/logout', { method: 'POST' }); showLogin(); } catch (error) { showError('chat-error', error.message); } });
  $('menu-button').addEventListener('click', () => { const opened = $('sidebar').classList.toggle('open'); $('sidebar-shade').hidden = !opened; $('menu-button').setAttribute('aria-expanded', String(opened)); });
  $('sidebar-shade').addEventListener('click', closeMenu);
  $('message-input').addEventListener('input', updateCount);
  $('message-input').addEventListener('keydown', event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); $('message-form').requestSubmit(); } });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') closeMenu(); });
  document.querySelectorAll('[data-prompt]').forEach(button => button.addEventListener('click', () => { if (busy) return; $('message-input').value = button.dataset.prompt; updateCount(); $('message-input').focus(); }));
  api('/api/auth/session').then(enter).catch(() => showLogin());
})();
