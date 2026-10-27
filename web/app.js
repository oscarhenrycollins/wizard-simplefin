'use strict';
// SimpleFIN, connector pattern: status card, the SimpleFIN connection (its rare actions in a sheet), your banks,
// one primary action, details. Setup is a four-step guide until a token is connected.
const el = (id) => document.getElementById(id);
const hosted = document.documentElement.classList.contains('wz-hosted');
let st = null, step = Number(sessionStorage.getItem('sf-step') || 1), poll = null, confirming = false;

function say(text, kind = '') { const s = el('status'); s.classList.toggle('wz-sr', !kind); s.classList.toggle('note', kind === 'note'); s.textContent = text; }
async function api(path, body) {
  const init = body === undefined ? { cache: 'no-store' } : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const r = await fetch(path, init);
  let out = {};
  try { out = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(out.error || 'Something went wrong. Try again.');
  return out;
}
function h(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
const money = (n, cur) => n == null ? '' : new Intl.NumberFormat(undefined, { style: 'currency', currency: cur || 'USD' }).format(n);
const when = (iso) => iso ? new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : 'not yet';
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;

function showStep(n) {
  step = n; sessionStorage.setItem('sf-step', n);
  for (let i = 1; i <= 4; i++) {
    const li = el('s' + i);
    li.classList.toggle('is-now', i === n);
    li.classList.toggle('is-done', i < n);
  }
}
function setPill(node, text, kind) { node.className = 'wz-pill ' + kind; node.textContent = text; }

function render(s) {
  st = s;
  const on = s.status === 'connected';
  el('setup').hidden = on; el('connected').hidden = !on;
  el('connect').hidden = on; el('add').hidden = !on; el('sync').hidden = !on; el('add-hint').hidden = !on;
  const attention = on && (!!s.error || s.banks.some((b) => b.status !== 'connected'));
  setPill(el('pill'), attention ? 'Needs attention' : on ? 'Connected' : 'Not connected', attention ? 'warn' : on ? 'ok' : 'off');
  renderDetails(s);
  if (!on) { showStep(step); closeSheet(); return; }
  el('sync').disabled = s.syncing;
  el('sync-note').textContent = s.syncing ? 'Syncing… the first sync can take a few minutes.'
    : `Last synced ${when(s.last_sync)} · ${s.transactions.toLocaleString()} transactions`;
  setPill(el('conn-pill'), s.syncing ? 'Syncing' : s.error ? 'Needs attention' : 'Active', s.error ? 'warn' : 'ok');
  el('err').hidden = !s.error; el('err').textContent = s.error || '';
  el('bank-count').textContent = s.banks.length;
  const box = el('banks'); box.replaceChildren();
  if (!s.banks.length && !s.syncing) box.append(h('li', 'sf-small sf-none', 'No banks yet. Link one in SimpleFIN, then tap Sync now.'));
  // One list row per bank at every width (connector pattern); its accounts sit under it, indented to the name.
  for (const b of s.banks) {
    const card = h('li', 'sf-bank');
    const t = h('div', 'wz-row sf-bank-h'), m = h('span', 'wz-main');
    const ok = b.status === 'connected';
    const tile = h('span', 'wz-tile', (b.institution || '?').trim().charAt(0).toUpperCase() || '?'); tile.setAttribute('aria-hidden', 'true');
    m.append(h('b', '', b.institution), h('small', '', plural(b.accounts.length, 'account') + (ok ? '' : ' · Needs attention in SimpleFIN')));
    t.append(tile, m, h('span', 'wz-pill ' + (ok ? 'ok' : 'warn'), ok ? 'Connected' : 'Needs attention'));
    card.append(t);
    if (b.error) card.append(h('div', 'sf-warn', b.error));
    const ul = h('ul', 'wz-list sf-accts');
    for (const a of b.accounts) {
      const li = h('li'), row = h('div', 'wz-row');
      const n = h('span', 'wz-main'); n.append(h('b', '', a.name), h('small', '', a.balance_at ? 'Balance as of ' + new Date(a.balance_at + 'T12:00:00').toLocaleDateString() : ''));
      row.append(n, h('span', 'sf-bal' + (a.balance < 0 ? ' neg' : ''), money(a.balance, a.currency)));
      li.append(row); ul.append(li);
    }
    card.append(ul); box.append(card);
  }
  if (el('conn-sheet').open) renderSheet();
  clearTimeout(poll);
  if (s.syncing) poll = setTimeout(load, 4000);
}
function renderDetails(s) {
  const box = el('details'); box.querySelectorAll('[data-live]').forEach((n) => n.remove());
  const add = (text) => { const p = h('p', '', text); p.dataset.live = ''; box.append(p); };
  if (s.connected_since) add('Connected since ' + when(s.connected_since));
  if (s.status === 'connected') {
    add('Last synced ' + when(s.last_sync) + ' · ' + s.transactions.toLocaleString() + ' transactions');
    add(`Syncs on its own every ${s.sync_every_hours} hours. SimpleFIN refreshes banks about once a day.`);
  }
  add('Contract: ' + s.contract);
}

// ---------- connection sheet (rare actions: disconnect) ----------
function openSheet() { confirming = false; renderSheet(); window.WzSheet ? WzSheet.open(el('conn-sheet')) : el('conn-sheet').showModal(); }
function closeSheet() { const d = el('conn-sheet'); if (!d.open) return; window.WzSheet ? WzSheet.close(d) : d.close(); }
function renderSheet() {
  const body = el('conn-sheet-body'); body.replaceChildren();
  const acts = h('div', 'wz-actions');
  if (confirming) {
    body.append(h('p', 'sf-sheet-note', 'Disconnect SimpleFIN? Your synced data is deleted from this box. To fully revoke access, also delete the "Wizard" app connection in SimpleFIN.'));
    const yes = h('button', 'wz-button', 'Disconnect'); yes.type = 'button'; yes.onclick = disconnect;
    const no = h('button', 'wz-button secondary', 'Cancel'); no.type = 'button'; no.onclick = () => { confirming = false; renderSheet(); };
    acts.append(yes, no); body.append(acts); return;
  }
  body.append(h('p', 'sf-sheet-note', el('sync-note').textContent));
  body.append(h('p', 'sf-sheet-note', 'Add or remove banks in SimpleFIN under Financial Institutions. They show up here at the next sync.'));
  const open = h('a', 'wz-button secondary', 'Open SimpleFIN ↗'); open.href = 'https://beta-bridge.simplefin.org/'; open.target = '_blank'; open.rel = 'noopener noreferrer';
  const off = h('button', 'wz-button secondary sf-danger', 'Disconnect'); off.type = 'button'; off.onclick = () => { confirming = true; renderSheet(); };
  acts.append(open, off); body.append(acts);
}
async function disconnect() {
  closeSheet();
  try { step = 1; render(await api('api/disconnect', {})); say('Disconnected.', 'note'); } catch (e) { say(e.message, 'error'); }
}

const load = () => api('api/status').then(render).catch((e) => say(e.message, 'error'));

document.querySelectorAll('[data-next]').forEach((b) => b.onclick = () => showStep(Number(b.dataset.next)));
for (let i = 1; i <= 4; i++) el('s' + i).querySelector('.sf-step-h').onclick = () => showStep(i);
// Connect is the one primary action: before step 4 it opens the token step, then it submits the token.
el('connect').addEventListener('click', (e) => { if (step !== 4) { e.preventDefault(); showStep(4); el('token').focus(); } });
el('token-form').onsubmit = async (e) => {
  e.preventDefault();
  const btn = el('connect'); btn.disabled = true; say('Connecting to SimpleFIN… this can take up to a minute.', 'note');
  try { render(await api('api/connect', { token: el('token').value })); el('token').value = ''; sessionStorage.removeItem('sf-step'); say('Connected. Pulling your accounts…', 'note'); }
  catch (err) { say(err.message, 'error'); }
  finally { btn.disabled = false; }
};
el('sync').onclick = async () => { try { render(await api('api/sync', {})); say('Syncing…', 'note'); } catch (e) { say(e.message, 'error'); } };
el('conn-row').onclick = openSheet;

if (!hosted) {
  history.pushState({ wzGuard: 1 }, '', location.href);
  addEventListener('popstate', () => { if (el('conn-sheet').open) closeSheet(); else if (step > 1 && st && st.status !== 'connected') showStep(step - 1); history.pushState({ wzGuard: 1 }, '', location.href); });
}
// Defined before wizard-sheet.js loads: an open sheet closes first on Back, then setup steps go back one.
window.__wizardCloseOverlay = () => { if (step > 1 && st && st.status !== 'connected') { showStep(step - 1); return true; } return false; };
load();
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
