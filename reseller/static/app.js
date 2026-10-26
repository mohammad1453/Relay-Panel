'use strict';
(function () {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // Links and forms are written as "/path"; prefix them when Relay runs under a base path.
  const basePath = document.body.dataset.basePath || '';
  if (basePath) {
    $$('[href^="/"],[action^="/"]').forEach(node => {
      for (const attr of ['href', 'action']) {
        const value = node.getAttribute(attr);
        if (value && value.startsWith('/') && !value.startsWith('//') && !value.startsWith(basePath + '/')) node.setAttribute(attr, basePath + value);
      }
    });
  }

  // Messages: close button, and success messages fade out on their own.
  $$('[data-dismiss]').forEach(btn => btn.addEventListener('click', () => btn.closest('.alert').remove()));
  $$('.alert-success').forEach(el => setTimeout(() => el.classList.add('fade'), 6000));
  // Remove ?message= / ?error= from the address bar so a reload doesn't show them again.
  if (location.search.match(/[?&](message|error)=/)) {
    const url = new URL(location.href);
    url.searchParams.delete('message'); url.searchParams.delete('error');
    history.replaceState(null, '', url.pathname + (url.search || '') + url.hash);
  }

  // Two-step confirm: the first click arms the button, a second click within 4s submits.
  $$('[data-confirm]').forEach(btn => {
    const original = btn.innerHTML;
    let timer = null;
    btn.addEventListener('click', event => {
      if (btn.classList.contains('armed')) return;
      event.preventDefault();
      btn.classList.add('armed');
      const label = btn.querySelector('span') || btn.querySelector('.confirm-text');
      if (label) label.textContent = btn.dataset.confirm; else btn.setAttribute('title', btn.dataset.confirm);
      btn.setAttribute('aria-label', btn.dataset.confirm);
      clearTimeout(timer);
      timer = setTimeout(() => { btn.classList.remove('armed'); btn.innerHTML = original; }, 4000);
    });
  });

  // Show that something is happening and prevent double submits.
  $$('form').forEach(form => form.addEventListener('submit', event => {
    if (event.defaultPrevented) return;
    const button = event.submitter;
    if (form.dataset.busy) { event.preventDefault(); return; }
    form.dataset.busy = '1';
    if (button) {
      setTimeout(() => {
        button.disabled = true;
        const label = button.querySelector('span');
        if (label && button.dataset.working) label.textContent = button.dataset.working;
        button.classList.add('busy');
      }, 0);
    }
  }));
  window.addEventListener('pageshow', () => $$('form[data-busy]').forEach(f => delete f.dataset.busy));

  // Sidebar on small screens.
  const toggleSidebar = open => document.body.classList.toggle('sidebar-open', open);
  $$('[data-toggle-sidebar]').forEach(b => b.addEventListener('click', () => toggleSidebar(!document.body.classList.contains('sidebar-open'))));
  $$('[data-close-sidebar]').forEach(b => b.addEventListener('click', () => toggleSidebar(false)));
  document.addEventListener('keydown', e => { if (e.key === 'Escape') toggleSidebar(false); });

  // Light / dark theme.
  $$('[data-theme-toggle]').forEach(btn => btn.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('relay-theme', next); } catch (e) { /* private mode */ }
  }));

  // Copy buttons.
  $$('[data-copy]').forEach(btn => btn.addEventListener('click', async () => {
    const field = document.getElementById(btn.dataset.copy);
    if (!field) return;
    const label = btn.querySelector('span');
    const before = label ? label.textContent : '';
    try { await navigator.clipboard.writeText(field.value); }
    catch (e) { field.focus(); field.select(); document.execCommand && document.execCommand('copy'); }
    btn.classList.add('copied');
    if (label) label.textContent = document.body.dataset.copied || 'Copied';
    setTimeout(() => { btn.classList.remove('copied'); if (label) label.textContent = before; }, 1800);
  }));

  // Show / hide password.
  $$('[data-reveal]').forEach(btn => btn.addEventListener('click', () => {
    const input = btn.parentElement.querySelector('input');
    input.type = input.type === 'password' ? 'text' : 'password';
    btn.classList.toggle('on', input.type === 'text');
  }));

  // Quick-pick chips.
  $$('[data-fill]').forEach(chip => chip.addEventListener('click', () => {
    const input = document.getElementById(chip.dataset.fill);
    input.value = chip.dataset.value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }));
  const pad = n => String(n).padStart(2, '0');
  const toLocal = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  $$('[data-add-days]').forEach(chip => chip.addEventListener('click', () => {
    const input = document.getElementById(chip.dataset.target);
    let base = new Date();
    if (!('fromNow' in chip.dataset) && input.value) {
      const current = new Date(input.value);
      if (!isNaN(current) && current > base) base = current;
    }
    base.setDate(base.getDate() + Number(chip.dataset.addDays));
    input.value = toLocal(base);
  }));

  // Fixed date vs. days after first use.
  const firstUse = $('#first-use');
  const fixed = $('#fixed-expiry'), duration = $('#duration');
  function expiryMode() {
    if (!firstUse) return;
    if (fixed) fixed.hidden = firstUse.checked;
    if (duration) duration.hidden = !firstUse.checked;
  }
  firstUse && firstUse.addEventListener('change', expiryMode);
  expiryMode();

  // Only show credential fields that apply to the chosen protocol.
  function credentials() {
    const chosen = $$('[data-protocol]:checked').map(e => e.dataset.protocol);
    $$('[data-protocols]').forEach(field => {
      const show = chosen.length === 0 ? true : chosen.some(p => field.dataset.protocols.split(' ').includes(p));
      field.hidden = !show;
      field.querySelectorAll('input').forEach(i => { i.disabled = !show; });
    });
  }
  $$('[data-protocol]').forEach(input => input.addEventListener('change', credentials));
  credentials();

  // Live plan summary on the customer form.
  const volume = $('#customer-volume');
  function summary() {
    if (!volume) return;
    const quota = Number(volume.dataset.quotaGb || 0);
    const current = Number(volume.dataset.currentGb || 0);
    const amount = Number(volume.value);
    const cost = Number.isFinite(amount) ? Math.max(0, amount - current) : 0;
    const left = quota - cost;
    const fmt = n => (Math.round(n * 100) / 100).toLocaleString('en-US') + ' GB';
    const costEl = $('#summary-cost'), leftEl = $('#summary-left'), err = $('#quota-error');
    if (costEl) costEl.textContent = volume.value ? fmt(cost) : '—';
    if (leftEl) { leftEl.textContent = volume.value ? fmt(left) : '—'; leftEl.classList.toggle('text-danger', left < 0); }
    if (err) err.hidden = !(volume.value && left < -1e-9);
    volume.setAttribute('aria-invalid', String(Boolean(volume.value && left < -1e-9)));
  }
  volume && volume.addEventListener('input', summary);
  summary();

  // Tabs without a page reload.
  $$('[data-tab]').forEach(tab => tab.addEventListener('click', event => {
    event.preventDefault();
    const key = tab.dataset.tab;
    $$('[data-tab]').forEach(t => { const on = t === tab; t.classList.toggle('tab-on', on); t.setAttribute('aria-selected', String(on)); });
    $$('.tab-panel').forEach(p => { p.hidden = p.id !== 'tab-' + key; });
    const url = new URL(location.href); url.searchParams.set('tab', key);
    history.replaceState(null, '', url.pathname + url.search);
  }));

  // Filters that apply as soon as they change.
  $$('[data-autosubmit]').forEach(el => el.addEventListener('change', () => el.form && el.form.submit()));
})();
