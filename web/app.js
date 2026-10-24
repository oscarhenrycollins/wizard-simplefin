'use strict';
const el = (id) => document.getElementById(id);
const hosted = document.documentElement.classList.contains('wz-hosted');
let st = null, step = Number(sessionStorage.getItem('sf-step') || 1), poll = null;

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

function showStep(n) {
  step = n; sessionStorage.setItem('sf-step', n);
  for (let i = 1; i <= 4; i++) {
    const li = el('s' + i);
    li.classList.toggle('is-now', i === n);
    li.classList.toggle('is-done', i < n);
  }
}

function render(s) {
  st = s;
  const on = s.status === 'connected';
  el('steps').hidden = on; el('connected').hidden = !on; el('lead').hidden = on;
  if (!on) { showStep(step); return; }
  el('sync').disabled = s.syncing;
  el('sync-note').textContent = s.syncing ? 'Syncing… the first sync can take a few minutes.'
    : `Last synced ${when(s.last_sync)} · ${s.transactions.toLocaleString()} transactions · SimpleFIN refreshes banks about once a day`;
  el('err').hidden = !s.error; el('err').textContent = s.error || '';
  const box = el('banks'); box.replaceChildren();
  if (!s.banks.length && !s.syncing) box.append(h('p', 'sf-small sf-empty', 'No banks yet. Link one in SimpleFIN, then tap Sync now.'));
  for (const b of s.banks) {
    const card = h('div', 'sf-bank');
    const t = h('div', 'sf-bank-h'); t.append(h('b', '', b.institution), h('small', '', b.status === 'connected' ? 'Connected' : 'Needs attention in SimpleFIN'));
    card.append(t);
    if (b.error) card.append(h('div', 'sf-warn', b.error));
    const ul = h('ul', 'sf-accts');
    for (const a of b.accounts) {
      const li = h('li');
      const n = h('span', 'sf-acct-n'); n.append(h('b', '', a.name), h('small', '', a.balance_at ? 'Balance as of ' + new Date(a.balance_at + 'T12:00:00').toLocaleDateString() : ''));
      li.append(n, h('span', 'sf-bal' + (a.balance < 0 ? ' neg' : ''), money(a.balance, a.currency)));
      ul.append(li);
    }
    card.append(ul); box.append(card);
  }
  clearTimeout(poll);
  if (s.syncing) poll = setTimeout(load, 4000);
}

const load = () => api('api/status').then(render).catch((e) => say(e.message, 'error'));

document.querySelectorAll('[data-next]').forEach((b) => b.onclick = () => showStep(Number(b.dataset.next)));
for (let i = 1; i <= 4; i++) el('s' + i).querySelector('.sf-step-h').onclick = () => showStep(i);
el('token-form').onsubmit = async (e) => {
  e.preventDefault();
  const btn = el('connect'); btn.disabled = true; say('Connecting to SimpleFIN… this can take up to a minute.', 'note');
  try { render(await api('api/connect', { token: el('token').value })); el('token').value = ''; sessionStorage.removeItem('sf-step'); say('Connected. Pulling your accounts…', 'note'); }
  catch (err) { say(err.message, 'error'); }
  finally { btn.disabled = false; }
};
el('sync').onclick = async () => { try { render(await api('api/sync', {})); say('Syncing…', 'note'); } catch (e) { say(e.message, 'error'); } };
el('disconnect').onclick = async () => {
  if (!confirm('Disconnect SimpleFIN? Your synced data is deleted from this box. To fully revoke access, also delete the "Wizard" app connection in SimpleFIN.')) return;
  try { step = 1; render(await api('api/disconnect', {})); say('Disconnected.', 'note'); } catch (e) { say(e.message, 'error'); }
};

if (!hosted) {
  history.pushState({ wzGuard: 1 }, '', location.href);
  addEventListener('popstate', () => { if (step > 1 && st && st.status !== 'connected') showStep(step - 1); history.pushState({ wzGuard: 1 }, '', location.href); });
}
window.__wizardCloseOverlay = () => { if (step > 1 && st && st.status !== 'connected') { showStep(step - 1); return true; } return false; };
load();
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
