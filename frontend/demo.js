'use strict';
const $ = id => document.getElementById(id);
let chat = null, manager = '', selected = '', draft = null, lastReply = null;
let clientBusy = false, clientPosting = false, managerPosting = false, refreshing = false;

async function api(path, {token, data, method = data === undefined ? 'GET' : 'POST'} = {}) {
  const headers = {};
  if (token) headers.Authorization = `Token ${token}`;
  if (data !== undefined) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, {method, headers, body: data === undefined ? undefined : JSON.stringify(data),
    cache: 'no-store', credentials: 'omit'});
  const result = await res.json();
  if (!res.ok) throw new Error(typeof result.detail === 'string' ? result.detail : `HTTP ${res.status}`);
  return result;
}
function on(id, event, fn, error) {
  $(id).addEventListener(event, async e => {
    e.preventDefault();
    $(error).textContent = '';
    try { await fn(e); } catch (err) { $(error).textContent = err.message; }
  });
}
const clientOn = (id, event, fn) => on(id, event, fn, 'client-error');
const managerOn = (id, event, fn) => on(id, event, fn, 'manager-error');
const clientPath = id => `/api/demo/chats/${id}`;
const managerPath = id => `/api/manager/chats/${id}`;

function markdown(parent, text) {
  // Small safe subset: links, emphasis, bold and inline code; raw HTML is always text.
  const pattern = /\[([^\]\n]+)\]\((https:\/\/[^\s)]+)\)|\*\*([^*\n]+)\*\*|\*([^*\n]+)\*|`([^`\n]+)`/g;
  let at = 0;
  for (const part of text.matchAll(pattern)) {
    parent.append(text.slice(at, part.index));
    let element;
    if (part[2]) {
      let url;
      try { url = new URL(part[2]); } catch { /* Render invalid URLs as text. */ }
      if (url && url.protocol === 'https:' && !url.username && !url.password &&
          ['store.softline.ru', 'docs.softline.com'].includes(url.hostname)) {
        element = document.createElement('a');
        element.href = url.href;
        element.target = '_blank';
        element.rel = 'noopener noreferrer';
        element.textContent = part[1];
      }
    } else {
      element = document.createElement(part[3] ? 'strong' : part[4] ? 'em' : 'code');
      element.textContent = part[3] || part[4] || part[5];
    }
    parent.append(element || part[0]);
    at = part.index + part[0].length;
  }
  parent.append(text.slice(at));
}
function renderLog(id, messages, scope) {
  const log = $(id);
  const version = `${scope}:${messages.map(item => item.id).join(',')}`;
  if (log.dataset.version === version) return;
  log.dataset.version = version;
  log.replaceChildren();
  if (!messages.length) {
    const row = document.createElement('li');
    row.textContent = 'Сообщений пока нет';
    log.append(row);
  }
  for (const item of messages) {
    const row = document.createElement('li');
    const name = document.createElement('small');
    name.textContent = ({client: 'Клиент', bot: 'Агент', manager: 'Менеджер'})[item.sender] || 'Система';
    if (item.sender === 'client') row.className = 'client-message';
    row.append(name);
    if (item.text) {
      const body = document.createElement('div');
      body.className = 'message-text';
      markdown(body, item.text);
      row.append(body);
    }
    if (item.pdf && /^\/pdfs\/kp_[a-f0-9]{32}\.pdf$/.test(item.pdf)) {
      const link = document.createElement('a');
      link.className = 'pdf';
      link.href = item.pdf;
      link.download = 'KP_Softline.pdf';
      link.textContent = 'Скачать PDF КП';
      row.append(document.createElement('br'), link);
    }
    log.append(row);
  }
  log.scrollTop = log.scrollHeight;
}
function clientControls() {
  $('send').querySelector('button').disabled = !chat || clientBusy || clientPosting;
  $('new').querySelector('button').disabled = clientPosting;
}
function quoteCard(snapshot, current) {
  const panel = $('quote');
  const key = snapshot?.confirmation_id || '';
  if (panel.dataset.key === key) return;
  panel.dataset.key = key;
  panel.replaceChildren();
  panel.hidden = !snapshot;
  if (!snapshot) return;
  const quote = snapshot.quote;
  const facts = document.createElement('p');
  facts.className = 'detail';
  facts.textContent = [...quote.items.map(x => `${x.name} × ${x.quantity} · ${x.subtotal} ₽`),
    `Скидка: ${quote.discount_percent}% · сумма: ${quote.total} ${quote.currency}`, quote.payment_terms,
    quote.delivery_terms].join('\n');
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'action'; button.textContent = 'Подтвердить состав КП';
  button.addEventListener('click', async () => {
    if (chat !== current || clientPosting || clientBusy) return;
    button.disabled = true; clientPosting = true; clientControls();
    try {
      await api(`${clientPath(current.chat_id)}/confirm-quote`, {token: current.token,
        data: {confirmation_id: key}});
      await api(`${clientPath(current.chat_id)}/messages`, {token: current.token,
        data: {text: 'Состав КП подтверждён. Сформируйте PDF.'}});
      await readClient();
    } catch (err) { $('client-error').textContent = err.message; button.disabled = false; }
    finally { clientPosting = false; clientControls(); }
  });
  panel.append(facts, button);
}
async function readClient() {
  const current = chat;
  if (!current) return;
  const view = await api(clientPath(current.chat_id), {token: current.token});
  if (current !== chat) return;
  clientBusy = view.busy;
  const labels = {bot: view.mode === 'offline' ? 'Офлайн · только подтверждённая БЗ' : 'Отвечает ИИ-агент',
    manager: 'Переведено на менеджера', escalated: 'Ожидает перевода на менеджера', closed: 'Диалог закрыт'};
  $('status').textContent = `${labels[view.status]}${view.busy ? ' · обрабатывается' : ''}`;
  renderLog('client-log', view.messages, current.chat_id);
  quoteCard(view.quote, current);
  clientControls();
}
async function newChat() {
  if (clientPosting) return;
  clientPosting = true; clientControls();
  try {
    chat = await api('/api/demo/chats', {data: {page: $('page').value}});
    $('question').value = '';
    await readClient();
  } finally { clientPosting = false; clientControls(); }
}
clientOn('new', 'submit', newChat);
clientOn('send', 'submit', async () => {
  if (!chat || clientPosting || clientBusy) return;
  const text = $('question').value.trim();
  if (!text) return;
  clientPosting = true; clientControls();
  try {
    await api(`${clientPath(chat.chat_id)}/messages`, {token: chat.token, data: {text}});
    $('question').value = '';
    await readClient();
  } finally { clientPosting = false; clientControls(); }
});
for (const button of document.querySelectorAll('[data-ask]')) {
  button.addEventListener('click', () => { $('question').value = button.dataset.ask; $('send').requestSubmit(); });
}

function lock() {
  manager = ''; selected = ''; lastReply = null; draft = null;
  for (const id of ['packets', 'metrics', 'details', 'qa', 'manager-error']) $(id).textContent = '';
  $('queue').replaceChildren(); renderLog('manager-log', [], '');
  $('answer').value = ''; $('token').value = '';
  $('workspace').hidden = $('lock').hidden = $('preview').hidden = true;
  $('access').hidden = false;
  $('manager-status').textContent = 'Закрыто · нужен отдельный токен';
}
function setTab(inspect) {
  $('ops').hidden = $('inspection').hidden = false;
  $(inspect ? 'ops' : 'inspection').hidden = true;
  $('ops-tab').setAttribute('aria-pressed', String(!inspect));
  $('inspect-tab').setAttribute('aria-pressed', String(inspect));
}
managerOn('access', 'submit', async () => {
  const token = $('token').value.trim();
  await api('/api/manager/escalations', {token});
  manager = token; $('token').value = '';
  $('workspace').hidden = $('lock').hidden = false;
  $('access').hidden = true;
  $('manager-status').textContent = 'Открыто · только демоэскалации';
  await readManager();
});
managerOn('lock', 'click', lock);
managerOn('ops-tab', 'click', async () => { setTab(false); await readManager(); });
managerOn('inspect-tab', 'click', async () => { setTab(true); await readManager(); });
managerOn('inspect-chat', 'change', readManager);
function selectChat(id) {
  if (selected !== id) {
    selected = id; draft = null; lastReply = null;
    $('answer').value = ''; $('preview').hidden = true;
  }
}
managerOn('queue', 'change', async () => { selectChat($('queue').value); await readManager(); });
function showUsage(usage) {
  const format = (n, digits) => n === null ? 'нет данных' : new Intl.NumberFormat('ru',
    {maximumSignificantDigits: digits}).format(n);
  $('metrics').textContent = [`Токены по измеренным запросам: ${format(usage.total_tokens, 10)}`,
    `Среднее на измеренный запрос: ${format(usage.average_tokens, 5)} (${usage.metered} из ${usage.requests})`,
    `Оценка на запрос: ${format(usage.average_cost_usd, 5)} USD / ${format(usage.average_cost_rub, 5)} ₽`,
    `Запросов с тарифом: ${usage.priced} из ${usage.requests}`].join('\n');
}
async function readManager() {
  const token = manager;
  if (!token) return;
  const {escalations} = await api('/api/manager/escalations', {token});
  if (manager !== token) return;
  if (!escalations.some(x => x.chat_id === selected)) selectChat(escalations[0]?.chat_id || '');
  const queue = $('queue');
  queue.replaceChildren(...escalations.map(x => new Option(`Диалог …${x.chat_id.slice(-8)}`, x.chat_id)));
  if (!escalations.length) queue.append(new Option('Нет активных эскалаций', ''));
  queue.value = selected;
  const id = selected;
  const inspect = !$('inspection').hidden;
  if (inspect) {
    const target = $('inspect-chat').value === 'selected' ? id : chat?.chat_id;
    if (!target) { $('metrics').textContent = 'Нет выбранного диалога'; $('packets').textContent = ''; return; }
    const view = await api(`${managerPath(target)}/inspector`, {token});
    if (manager !== token || $('inspection').hidden || target !== ($('inspect-chat').value === 'selected' ? selected : chat?.chat_id)) return;
    showUsage(view.usage); $('packets').textContent = JSON.stringify(view.events, null, 2);
    return;
  }
  const view = id ? await api(managerPath(id), {token}) : null;
  if (manager !== token || id !== selected) return;
  const detail = view?.escalation;
  $('details').textContent = detail ? `Причина: ${detail.reason}\nСаммари: ${detail.summary}\nСлоты: ${JSON.stringify(detail.slots)}` : 'Ожидаем перевод диалога';
  renderLog('manager-log', view?.messages || [], id);
  if (!lastReply && view) lastReply = view.messages.findLast(item => item.sender === 'manager')?.id || null;
  $('reply').querySelector('button').disabled = managerPosting || view?.status !== 'manager';
  $('draft').disabled = managerPosting || !lastReply;
}
async function managerWrite(fn) {
  if (managerPosting) return;
  managerPosting = true;
  try { await fn(); } finally { managerPosting = false; await readManager(); }
}
managerOn('reply', 'submit', () => managerWrite(async () => {
  const id = selected, token = manager, text = $('answer').value.trim();
  if (!id || !text) return;
  const result = await api(`${managerPath(id)}/reply`, {token, data: {text}});
  if (selected === id && manager === token) {
    lastReply = result.reply_id; $('answer').value = ''; draft = null; $('preview').hidden = true;
  }
  await readClient();
}));
managerOn('draft', 'click', () => managerWrite(async () => {
  const id = selected, token = manager;
  if (!id || !lastReply) return;
  const result = await api('/api/manager/kb-from-reply', {token, data: {chat_id: id, reply_id: lastReply}});
  if (selected !== id || manager !== token) return;
  draft = result;
  $('qa').textContent = `${draft.mode === 'offline' ? 'Офлайн · без генерации' : 'Сформировано LLM'}\nВопрос: ${draft.question}\nОтвет: ${draft.answer}`;
  $('preview').hidden = false;
}));
managerOn('discard', 'click', () => { draft = null; $('preview').hidden = true; });
managerOn('confirm', 'click', () => managerWrite(async () => {
  const snapshot = draft, token = manager;
  if (!snapshot) return;
  const result = await api(`/api/manager/kb-drafts/${snapshot.id}/confirm`, {token, method: 'POST'});
  if (draft !== snapshot || manager !== token) return;
  draft = null; $('preview').hidden = true;
  $('manager-error').textContent = `Статья опубликована · ${result.search === 'hybrid' ? 'гибридный поиск' : 'FTS без эмбеддинга'}. Проверьте вопрос в новом диалоге.`;
}));
async function refresh() {
  if (refreshing || document.hidden) return;
  refreshing = true;
  try {
    const updates = [];
    if (!clientPosting) updates.push(readClient().catch(err => { $('client-error').textContent = err.message; }));
    if (!managerPosting) updates.push(readManager().catch(err => { $('manager-error').textContent = err.message; }));
    await Promise.all(updates);
  } finally { refreshing = false; }
}
clientControls();
newChat().catch(err => { $('client-error').textContent = err.message; $('status').textContent = 'Демо недоступно'; });
setInterval(refresh, 850);
