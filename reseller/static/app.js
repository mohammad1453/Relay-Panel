'use strict';
const basePath=document.body.dataset.basePath||'';
if(basePath){document.querySelectorAll('[href^="/"],[action^="/"]').forEach(node=>{for(const attr of ['href','action']){const value=node.getAttribute(attr);if(value?.startsWith('/'))node.setAttribute(attr,basePath+value);}});}
document.querySelectorAll('input,select,textarea').forEach(field=>{field.addEventListener('invalid',()=>field.setCustomValidity(document.body.dataset.invalid));field.addEventListener('input',()=>field.setCustomValidity(''));});
document.querySelectorAll('[data-confirm]').forEach(form=>form.addEventListener('submit',e=>{if(!confirm(form.dataset.confirm))e.preventDefault();}));
const quotaField=document.getElementById('customer-volume');
const quotaError=document.getElementById('quota-error');
function checkCustomerQuota(){if(!quotaField||quotaField.dataset.checkQuota!=='true')return false;const limit=Number(quotaField.dataset.quotaGb),amount=Number(quotaField.value);const exceeds=Number.isFinite(limit)&&Number.isFinite(amount)&&amount>limit+1e-9;quotaError.hidden=!exceeds;const hint=document.querySelector('.quota-feedback .field-hint');if(hint)hint.hidden=exceeds;quotaField.setAttribute('aria-invalid',String(exceeds));return exceeds;}
quotaField?.addEventListener('input',checkCustomerQuota);
document.getElementById('customer-form')?.addEventListener('submit',e=>{if(checkCustomerQuota()){e.preventDefault();quotaError.scrollIntoView({block:'nearest',behavior:'smooth'});quotaField.focus();}});
document.querySelectorAll('form[method="post"]').forEach(form=>form.addEventListener('submit',e=>{if(e.defaultPrevented)return;form.setAttribute('aria-busy','true');const b=e.submitter;if(b){setTimeout(()=>{b.disabled=true;b.textContent=b.dataset.working||'…';},0);}}));
document.querySelectorAll('[data-tab]').forEach(button=>button.addEventListener('click',()=>{document.querySelectorAll('.tab-content').forEach(s=>s.hidden=s.id!==button.dataset.tab);document.querySelectorAll('[data-tab]').forEach(b=>{b.classList.toggle('active',b===button);b.setAttribute('aria-selected',String(b===button));});}));
document.querySelectorAll('[data-copy]').forEach(button=>button.addEventListener('click',async()=>{const field=document.getElementById(button.dataset.copy);try{await navigator.clipboard.writeText(field.value);button.textContent=button.dataset.copied;}catch{field.focus();field.select();}}));
const first=document.getElementById('first-use');
function expiry(){if(!first)return;document.getElementById('fixed-expiry').hidden=first.checked;document.getElementById('duration').hidden=!first.checked;document.querySelector('[name="expiry"]').required=!first.checked;document.querySelector('[name="days"]').required=first.checked;document.querySelector('[name="expiry"]').setCustomValidity('');document.querySelector('[name="days"]').setCustomValidity('');}
first?.addEventListener('change',expiry);expiry();
function credentials(){const protocols=[...document.querySelectorAll('[data-protocol]:checked')].map(e=>e.dataset.protocol);document.querySelectorAll('[data-protocols]').forEach(label=>{label.hidden=!protocols.some(p=>label.dataset.protocols.split(' ').includes(p));label.querySelectorAll('input').forEach(i=>i.disabled=label.hidden);});}
document.querySelectorAll('[data-protocol]').forEach(input=>input.addEventListener('change',credentials));credentials();
document.getElementById('customer-form')?.addEventListener('invalid',e=>{const pane=e.target.closest('.tab-content');if(pane?.hidden)document.querySelector(`[data-tab="${pane.id}"]`)?.click();},true);
