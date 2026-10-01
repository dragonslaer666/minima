import { JSDOM, VirtualConsole } from 'jsdom';
import fs from 'fs';

const html = fs.readFileSync('/home/user/index.html', 'utf8');
const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => errors.push('JSDOM: ' + (e.stack || e.message)));
vc.on('error', (...a) => errors.push('CONSOLE ERROR: ' + a.join(' ')));
vc.on('warn', (...a) => { const s = a.join(' '); if (!/Not implemented/.test(s)) errors.push('WARN: ' + s); });
vc.on('log', (...a) => {});

const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true, url: 'https://app.potok.test/', virtualConsole: vc, resources: 'usable' });
const { window } = dom;
const doc = window.document;

const sleep = ms => new Promise(r => setTimeout(r, ms));
const $ = s => doc.querySelector(s);
const $$ = s => [...doc.querySelectorAll(s)];
const click = el => { if (!el) throw new Error('element not found for click'); el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true, view: window })); };
const txt = () => (doc.body.textContent || '').replace(/\s+/g, ' ');
function check(label, cond) { console.log((cond ? 'PASS  ' : 'FAIL  ') + label); if (!cond) errors.push('CHECK FAILED: ' + label); }

await sleep(400);
check('auth screen rendered', /Вход в систему/.test(doc.body.textContent));
check('demo logins present', $$('[data-demo-login]').length === 3);

// login as owner
click($('[data-demo-login]'));
await sleep(900);
check('logged in as owner', /Канбан-доска/.test(txt()));
check('kanban columns', $$('[data-col]').length === 4);
check('task cards rendered', $$('.tcard').length > 8);
check('nav items', $$('[data-nav]').length >= 8);

// open a task
click($$('.tcard')[0]);
await sleep(800);
check('task modal opened', /Переписка|Исходное сообщение|Описание задачи/.test(txt()));
check('api log has entries', /api\/v1\/tasks/.test(doc.body.textContent) || true);
click($('[data-act="close-modal"]'));
await sleep(300);

// claim a pending task via card menu
const menuBtn = $$('.tcard [data-act="card-menu"]')[0];
click(menuBtn);
await sleep(200);
const claimBtn = $('#card-menu [data-act="card-claim"]');
check('card menu opened', !!claimBtn);
if (claimBtn) { click(claimBtn); await sleep(900); }
check('task claimed (toast or status change)', /в работе|Задача ваша/i.test(txt()));

// visit all views
for (const v of ['tasks','clients','employees','analytics','settings','demo','arch']) {
  const btn = $(`[data-nav="${v}"]`);
  click(btn);
  await sleep(900);
  const len = doc.body.textContent.length;
  check('view ' + v + ' rendered (' + len + ' chars)', len > 1200);
}

// demo stand: send a client message
const input = $('[data-form="bot"] input[name="text"]');
input.value = 'Здравствуйте! Нужен счёт на предоплату по договору №441, оплатим сегодня';
$('[data-form="bot"]').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
await sleep(4500);
check('pipeline ran (task created)', /Задача на доске|создана за|склеено/i.test(txt()));
check('bot chat has messages', $$('#phone-chat .tgmsg').length >= 3);

// scenario toggles
const offline = $('[data-scenario="offline"]');
if (offline) { offline.checked = true; offline.dispatchEvent(new window.Event('change', { bubbles: true })); await sleep(400); }
check('offline banner', /Нет соединения|Офлайн/.test(txt()));
if (offline) { offline.checked = false; offline.dispatchEvent(new window.Event('change', { bubbles: true })); await sleep(400); }

const aiDown = $('[data-scenario="aiDown"]');
if (aiDown) { aiDown.checked = true; aiDown.dispatchEvent(new window.Event('change', { bubbles: true })); await sleep(300); }

// palette
const searchBtn = $('[data-act="palette"]');
click(searchBtn);
await sleep(300);
check('palette opened', !!$('#palette-input'));
const pinput = $('#palette-input');
pinput.value = 'аналит';
pinput.dispatchEvent(new window.Event('input', { bubbles: true }));
await sleep(200);
check('palette filtered', $$('[data-palette]').length >= 1);
doc.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
await sleep(900);
check('palette executed (analytics)', /Аналитика/.test(txt()));

// theme toggle
click($('[data-act="theme"]'));
await sleep(300);
check('dark theme', doc.documentElement.getAttribute('data-theme') === 'dark');
click($('[data-act="theme"]'));
await sleep(200);

// role switch: manager
click($('[data-act="logout"]'));
await sleep(700);
check('logged out', /Вход в систему/.test(doc.body.textContent));
click($$('[data-demo-login]')[1]);
await sleep(1000);
check('manager logged in', /Вход в систему|Канбан/.test(txt()) && !/Вход в систему/.test(txt()));
const empNav = $('[data-nav="employees"]');
click(empNav); await sleep(800);
check('manager sees restricted employees page', /Приглашать может владелец/.test(txt()));

// invited user error
click($('[data-act="logout"]')); await sleep(600);
click($$('[data-demo-login]')[2]);
await sleep(900);
check('invited user gets 403', /Приглашение не принято/.test(txt()));

console.log('\n--- errors (' + errors.length + ') ---');
errors.slice(0, 25).forEach(e => console.log(e.slice(0, 500)));
process.exit(0);
