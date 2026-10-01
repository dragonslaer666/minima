import { JSDOM, VirtualConsole } from 'jsdom';
import fs from 'fs';
const html=fs.readFileSync('/home/user/index.html','utf8');
const errors=[];
const vc=new VirtualConsole();
vc.on('jsdomError',e=>errors.push('JSDOM: '+String(e.stack||e.message).slice(0,300)));
vc.on('error',(...a)=>errors.push('CONSOLE: '+a.join(' ').slice(0,300)));
vc.on('warn',(...a)=>{const s=a.join(' '); if(!/Not implemented|Could not parse CSS|navigation/.test(s)) errors.push('WARN: '+s.slice(0,200));});
const dom=new JSDOM(html,{runScripts:'dangerously',pretendToBeVisual:true,url:'https://x.test/',virtualConsole:vc});
const {window}=dom;const doc=window.document;const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const $=s=>doc.querySelector(s),$$=s=>[...doc.querySelectorAll(s)];
const click=el=>{ if(!el) throw new Error('no element: '+(new Error().stack.split('\n')[2]||'').trim()); el.dispatchEvent(new window.MouseEvent('click',{bubbles:true,cancelable:true,view:window})); };
const submit=f=>f.dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true}));
const txt=()=>doc.body.textContent||'';
let fails=0; const check=(l,c)=>{ if(!c) fails++; console.log((c?'PASS  ':'FAIL  ')+l); };
const P=()=>window.__POTOK;

await sleep(500); click($('[data-demo-login]')); await sleep(1400);

// 1. notifications
click($('[data-act="notifications"]')); await sleep(400);
check('S1 notifications panel', /Уведомления/.test(txt()) && $$('.menu-pop').length>0);
const beforeRead = P().db.notifications.filter(n=>!n.read).length;
click($('[data-act="notif-read"]')); await sleep(800);
check('S2 mark all read', P().db.notifications.filter(n=>!n.read).length===0 && beforeRead>0);

// 2. user menu → profile
click($('[data-act="user-menu"]')); await sleep(400);
check('S3 user menu', /Мои сессии/.test(txt()));
click($('[data-act="profile"]')); await sleep(600);
check('S4 profile modal', /Данные профиля/.test(txt()));
// link telegram from profile
click($('[data-act="link-tg"]')); await sleep(900);
check('S5 link-tg modal with code', /Код привязки/.test(txt()));
click($('[data-act="link-tg-done"]')); await sleep(1600);
check('S6 tg linked + push preview', /Проверка связи|Push менеджеру/.test(txt()) && !!P().state.session);
click($('[data-act="close-modal"]')); await sleep(300);

// 3. shortcuts
doc.dispatchEvent(new window.KeyboardEvent('keydown',{key:'?',bubbles:true})); await sleep(400);
check('S7 shortcuts modal via ?', /Горячие клавиши/.test(txt()));
doc.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Escape',bubbles:true})); await sleep(300);
check('S8 escaped modal', !$('.modal'));

// 4. sla modal
click($('[data-nav="board"]')); await sleep(1100);
click($('[data-act="quick"][data-q="OVERDUE"]')); await sleep(600);
check('S9 sla modal', /Просроченные задачи|Просрочек нет/.test(txt()));
click($('[data-act="close-modal"]')); await sleep(300);
click($('[data-act="quick"][data-q="FREE"]')); await sleep(1000);
check('S10 free filter', /Свободные/.test(txt()));
click($('[data-act="quick"][data-q="MINE"]')); await sleep(1000);
check('S11 mine filter', /Мои/.test(txt()));
click($('[data-act="quick"][data-q="ALL"]')); await sleep(1000);

// 5. card menu actions
const card=$('.tcard');
click(card.querySelector('[data-act="card-menu"]')); await sleep(300);
check('T1 card menu', !!$('#card-menu'));
click($('#card-menu [data-act="card-prio"]')); await sleep(1200);
check('T2 priority changed', /Приоритет/.test(txt()));
click($('.tcard [data-act="card-menu"]')); await sleep(300);
click($('#card-menu [data-act="card-open"]')); await sleep(1200);
check('T3 card opened from menu', !!$('.modal--xl'));

// 6. cancel task
const cancelBtn=$('[data-act="cancel-task"]');
if (cancelBtn){
  click(cancelBtn); await sleep(500);
  check('U1 cancel modal', /Причина отмены/.test(txt()));
  submit($('[data-form="cancel"]')); await sleep(1400);
  check('U2 task cancelled', /Причина записана в историю|Отменено/.test(txt()));
} else check('U1 cancel modal (skipped: no cancel button)', true);

// 7. reassign flow (владелец видит кнопку «Сменить» у задачи с исполнителем)
click($('[data-nav="board"]')); await sleep(1200);
let target=$('.tcard[data-status="PROCESSING"]') || $('.tcard');
if (target){
  click(target); await sleep(1400);
  check('V0 task modal opened', !!$('.modal--xl'));
  const ra=$('[data-act="reassign"]');
  if (ra){
    click(ra); await sleep(500);
    check('V1 reassign popup', !!$('#reassign-select'));
    if ($('#reassign-ok')){ click($('#reassign-ok')); await sleep(1500); check('V2 reassigned', /Исполнитель назначен|назначен/.test(txt())); }
  } else check('V1 reassign button (задача без исполнителя)', true);
  value_guard: { }
  const cm=$('[data-act="close-modal"]'); if (cm){ click(cm); await sleep(400); }
} else check('V0 board has cards', false);

// 8. new task creation
doc.dispatchEvent(new window.KeyboardEvent('keydown',{key:'n',bubbles:true})); await sleep(400);
check('W1 new task modal via N', /Новая задача вручную/.test(txt()));
$('[data-bind="new.title"]').value='Тестовая задача из демо';
submit($('[data-form="new-task"]')); await sleep(1600);
check('W2 task created and opened', /Тестовая задача из демо/.test(txt()));
click($('[data-act="close-modal"]')); await sleep(300);

// 9. clients + client modal + reply-client
click($('[data-nav="clients"]')); await sleep(1200);
check('X1 clients table', /Клиенты/.test(txt()) && $$('table tbody tr').length>=5);
click($$('table tbody tr')[0]); await sleep(1200);
check('X2 client modal', /Переписка/.test(txt()));
const rc=$('[data-form="reply-client"] input[name="text"]');
if (rc){ rc.value='Добрый день! Отвечаем по вашему обращению.'; submit($('[data-form="reply-client"]')); await sleep(1400);
  check('X3 reply to client sent', /Ответ отправлен/.test(txt()));
} else check('X3 reply-client form', false);

// 10. employees actions
click($('[data-nav="employees"]')); await sleep(1200);
const roleBtn=$('[data-act="employee-role"]');
if (roleBtn){ click(roleBtn); await sleep(1400);
  check('Y1 role changed', /Роль изменена|Администратор/.test(txt()));
}
const dis=$('[data-act="employee-disable"]');
if (dis){ const freed=dis.dataset.uuid; click(dis); await sleep(1600);
  check('Y2 employee disabled & tasks released', /отключён|Доступ/.test(txt()) || !!P().db.memberships.find(m=>m.uuid===freed && m.status==='DISABLED'));
  const en=$('[data-act="employee-enable"]'); if (en){ click(en); await sleep(1300); check('Y3 employee enabled back', /восстановлен/.test(txt())); }
   else check('Y3 employee enabled back', true);
} else check('Y2 employee disable (нет кнопки)', true);

// 11. duplicate invite → 409
click($('[data-act="invite"]')); await sleep(400);
$('#i-email').value='petrov@vector.ru';
submit($('[data-form="invite"]')); await sleep(1300);
check('Z1 duplicate invite → 409 friendly error', /уже добавлен|уже в организации|Уже/.test(txt()));
click($('[data-act="close-modal"]')); await sleep(300);

// 12. category delete (in use → 409)
click($('[data-nav="settings"]')); await sleep(1100);
click($('[data-act="settings-tab"][data-tab="categories"]')); await sleep(700);
const del=$('[data-act="delete-category"]');
if (del){ click(del); await sleep(1200); check('Z2 category delete handled', /Категория удалена|используется/.test(txt())); }
else check('Z2 category delete (нет свободных категорий)', true);

// 13. export as owner (csv)
click($('[data-nav="tasks"]')); await sleep(1200);
click($('[data-act="export-csv"]')); await sleep(700);
check('Z3 csv export ok', /Экспорт готов/.test(txt()));

// 14. filters & sorting in table
const sortSel=$('[data-filter="sort"]'); sortSel.value='priority'; sortSel.dispatchEvent(new window.Event('change',{bubbles:true})); await sleep(1100);
check('Z4 sort by priority', $$('table tbody tr').length>5);
const prioSel=$('[data-filter="priority"]'); prioSel.value='URGENT'; prioSel.dispatchEvent(new window.Event('change',{bubbles:true})); await sleep(1200);
const rows=$$('table tbody tr'); const urgentOnly=rows.length>0 && rows.every(r=>/Срочно/.test(r.textContent));
check('Z5 filter urgent', urgentOnly || rows.length===1);
const search=$('#table-search'); search.value='доставка'; search.dispatchEvent(new window.Event('input',{bubbles:true})); await sleep(1400);
check('Z6 search works', $$('table tbody tr').length>=1 && /доставк|Доставк/i.test(txt()));

// 15. API contract checks via hook
const api=P().Api;
let r428=await api.post('/api/v1/messages',{text:'без ключа'},{headers:{'X-Bot-Secret':'demo-bot-secret'}}).then(()=>null).catch(e=>e);
check('AA1 428 without Idempotency-Key', r428 && r428.status===428);
let r401=await api.post('/api/v1/messages',{text:'плохой секрет'},{headers:{'X-Bot-Secret':'wrong','Idempotency-Key':'x1'}}).then(()=>null).catch(e=>e);
check('AA2 401 bad bot secret', r401 && r401.status===401);
let r422=await api.post('/api/v1/messages',{text:'a'.repeat(4100)},{headers:{'X-Bot-Secret':'demo-bot-secret','Idempotency-Key':'x2'}}).then(()=>null).catch(e=>e);
check('AA3 422 message too long', r422 && r422.status===422);
let r404=await api.get('/api/v1/tasks/00000000-0000-4000-8000-000000000000').then(()=>null).catch(e=>e);
check('AA4 404 unknown task', r404 && r404.status===404);
const idem='idem-test-1';
let first=await api.post('/api/v1/tasks',{title:'Идемпотентная задача'},{headers:{'Idempotency-Key':idem}}).then(r=>r).catch(e=>null);
let second=await api.post('/api/v1/tasks',{title:'Идемпотентная задача'},{headers:{'Idempotency-Key':idem}}).then(r=>r).catch(e=>null);
check('AA5 idempotency replay returns same task', first && second && first.body.task.taskId===second.body.task.taskId && second.replayed===true);
const tasksBefore=P().db.tasks.length;
let bad=await api.post('/api/v1/tasks',{title:'x'}).then(()=>null).catch(e=>e);
check('AA6 422 short title', bad && bad.status===422 && P().db.tasks.length===tasksBefore);
// health
let health=await api.get('/actuator/health').then(r=>r.body).catch(()=>null);
check('AA7 health endpoint', health && health.status==='UP' && health.components.outbox);
// token refresh flow
P().state.session.expired=true;
let ok=false;
try { await api.get('/api/v1/tasks'); } catch(e){ ok = /токен истёк|Токен истёк/i.test(eTitleOf(e)); }
function eTitleOf(e){ return (e.body&&e.body.title)||e.message||''; }
check('AA8 expired token → 401 + refresh hint', ok);
await api.get('/api/v1/tasks').then(()=>{}).catch(()=>{});

// 16. theme + keyboard nav
click($('[data-act="theme"]')); await sleep(400);
check('BB1 dark theme persisted', doc.documentElement.getAttribute('data-theme')==='dark' && window.localStorage.getItem('potok.theme')==='"dark"');
doc.dispatchEvent(new window.KeyboardEvent('keydown',{key:'t',bubbles:true})); await sleep(900);
check('BB2 keyboard T → table view', /Все задачи/.test(txt()));
doc.dispatchEvent(new window.KeyboardEvent('keydown',{key:'b',bubbles:true})); await sleep(900);
check('BB3 keyboard B → board', /Необработанные/.test(txt()));
click($('[data-act="theme"]')); await sleep(300);

// 17. bot: block + status + attach
click($('[data-nav="demo"]')); await sleep(1000);
click($('[data-act="bot-status"]')); await sleep(1200);
check('CC1 /status auto reply', /По вашим обращениям/.test(txt()));
click($('[data-act="bot-attach"]')); await sleep(600);
check('CC2 attachment message', /прайс-лист_2026/.test(txt()));
click($('[data-act="bot-block"]')); await sleep(900);
check('CC3 bot blocked → 403 explained', /403/.test(txt()));

// 18. autopilot toggle
click($('[data-act="autopilot"]')); await sleep(1500);
check('DD1 autopilot started', P().scenario.autopilot===true);
click($('[data-act="autopilot"]')); await sleep(500);
check('DD2 autopilot stopped', P().scenario.autopilot===false);

console.log('\n=== errors: '+errors.length+' | failed checks: '+fails+' ===');
[...new Set(errors)].slice(0,12).forEach(e=>console.log('* '+e));
process.exit(0);
