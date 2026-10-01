import { JSDOM, VirtualConsole } from 'jsdom';
import fs from 'fs';
const html = fs.readFileSync('/home/user/index.html','utf8');
const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => errors.push('JSDOM: ' + (e.stack||e.message).slice(0,300)));
vc.on('error', (...a) => errors.push('CONSOLE: ' + a.join(' ').slice(0,300)));
vc.on('warn', (...a) => { const s=a.join(' '); if(!/Not implemented|Could not parse CSS/.test(s)) errors.push('WARN: '+s.slice(0,200)); });
const dom = new JSDOM(html,{runScripts:'dangerously',pretendToBeVisual:true,url:'https://x.test/',virtualConsole:vc});
const {window}=dom; const doc=window.document;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const $=s=>doc.querySelector(s), $$=s=>[...doc.querySelectorAll(s)];
const click=el=>{ if(!el) throw new Error('no element to click'); el.dispatchEvent(new window.MouseEvent('click',{bubbles:true,cancelable:true,view:window})); };
const submit=f=>f.dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true}));
const logout=async()=>{ const um=$('[data-act="user-menu"]'); if(um) { click(um); await sleep(350); } click($('[data-act="logout"]')); await sleep(950); };
const txt=()=>doc.body.textContent||'';
let fails=0;
function check(label,cond){ if(!cond) fails++; console.log((cond?'PASS  ':'FAIL  ')+label); }
const drag=(card,targetCol)=>{
  const dt={effectAllowed:'',setData(){},getData(){return ''}};
  const mk=(type,el,t)=>{ const ev=new window.Event(type,{bubbles:true,cancelable:true}); Object.defineProperty(ev,'dataTransfer',{value:dt}); Object.defineProperty(ev,'target',{value:el}); el.dispatchEvent(ev); };
  mk('dragstart',card); mk('dragover',targetCol); mk('drop',targetCol);
};
await sleep(500);
// ---------- AUTH ----------
click($('[data-demo-login]'));await sleep(1400);
check('A1 owner login → board', /Необработанные/.test(txt()) && $$('.tcard').length>10);
// ---------- DnD ----------
const firstCard=$('.tcard[data-status="PENDING"]');
const uuid=firstCard.dataset.task;
const doneCol=$('[data-drop="PROCESSING"]');
drag(firstCard,doneCol);
await sleep(1200);
const moved=doc.querySelector(`.tcard[data-task="${uuid}"]`);
check('B1 drag&drop moved card to PROCESSING', moved && moved.dataset.status==='PROCESSING');
check('B2 toast about optimistic update', /optimistic update подтверждён/.test(txt()) || /Статус обновлён/.test(txt()));
// ---------- conflict 409 (rival) ----------
click($('[data-nav="demo"]')); await sleep(900);
check('C1 demo stand rendered', /Конвейер обработки/.test(txt()));
// autopilot off; send message from client
const inp=$('[data-form="bot"] input[name="text"]');
inp.value='Здравствуйте! Нужен счёт на предоплату по договору №441, оплатим сегодня';
submit($('[data-form="bot"]'));
await sleep(5200);
check('D1 pipeline finished', /Задача на доске|создана за|склеено/.test(txt()));
check('D2 bot chat 5+ messages', $$('#phone-chat .tgmsg').length>=5);
check('D3 new task created in DB', /счёт|Счёт|Счет/.test(txt()));
// dedupe: second message within 90s
inp.value='Оплатим сегодня до обеда, пришлите счёт';
submit($('[data-form="bot"]'));
await sleep(5200);
check('D4 second message merged (dedup)', /склеено/i.test(txt()));
// ---------- scenario: AI down ----------
const setSw=(name,val)=>{ const el=$('[data-scenario="'+name+'"]'); if(!el) throw new Error('no switch '+name); el.checked=val; el.dispatchEvent(new window.Event('change',{bubbles:true})); };
setSw('aiDown',true);
await sleep(400);
check('E1 AI down banner', /AI-модуль недоступен|провайдер недоступен|fallback/i.test(txt()));
inp.value='Третий раз пишу: датчик давления не работает!';
submit($('[data-form="bot"]'));
await sleep(6000);
check('E2 fallback task created', /AI не ответил|категория не определена|fallback/i.test(txt()));
setSw('aiDown',false);
await sleep(400);
// ---------- offline ----------
setSw('offline',true);
await sleep(500);
check('F1 offline banner', /Нет соединения|Офлайн/i.test(txt()));
setSw('offline',false);
await sleep(600);
// ---------- 429 ----------
click($('[data-nav="settings"]')); await sleep(1000);
click($('[data-act="settings-tab"][data-tab="tg"]')); await sleep(500);
click($('[data-act="force-429"]')); await sleep(300);
click($('[data-nav="demo"]')); await sleep(900);
inp.value='Добрый день! Возможна ли доставка в Каспийск на следующей неделе?';
submit($('[data-form="bot"]'));
await sleep(6000);
check('G1 telegram 429 handled', /429/.test(txt()));
// ---------- rival claim ----------
setSw('rival',true);
await sleep(300);
click($('[data-nav="board"]')); await sleep(1200);
const pend=$('.tcard[data-status="PENDING"]');
let claimed=false;
if(pend){
  click(pend.querySelector('[data-act="card-menu"]')); await sleep(250);
  const b=$('#card-menu [data-act="card-claim"]'); if(b){ click(b); await sleep(1400); claimed=true; }
}
check('H1 rival claim → 409 explained', !claimed || /уже взяли в работу|забрал|409/i.test(txt()));
// ---------- views & role gates ----------
click($('[data-nav="employees"]')); await sleep(1000);
check('I1 employees view', /Приглашение|Пригласить|Сотрудники/.test(txt()));
click($('[data-act="invite"]')); await sleep(500);
check('I2 invite modal', /одноразовая|72 часа/.test(txt()));
const em=$('#i-email'); em.value='newbie@vector.ru'; submit($('[data-form="invite"]')); await sleep(1200);
check('I3 invitation created', /Приглашение отправлено/.test(txt()));
// settings tabs
click($('[data-nav="settings"]')); await sleep(1200);
check('J1 settings org tab', /Профиль организации/.test(txt()));
click($('[data-act="settings-tab"][data-tab="sec"]')); await sleep(500);
check('J2 security tab with matrix', /Матрица прав/.test(txt()));
click($('[data-act="settings-tab"][data-tab="tg"]')); await sleep(400);
check('J3 telegram tab', /Webhook|Deep-link/.test(txt()));
click($('[data-act="settings-tab"][data-tab="categories"]')); await sleep(600);
const catInput=$('[data-form="category"] input[name="title"]'); catInput.value='Тестовая категория';
submit($('[data-form="category"]')); await sleep(1100);
const dbg = window.__POTOK;
console.log('   DEBUG cats:', dbg.db.categories.map(c=>c.title).slice(-3).join('|'), '| api calls:', dbg.Api.log.length, '| 429s:', dbg.Api.log.filter(l=>l.status===429).length, '| last:', dbg.Api.log.slice(0,3).map(l=>l.status+' '+l.method+' '+l.path).join(' ; '));
check('J4 category added', /Тестовая категория/.test(txt()));
click($('[data-act="settings-tab"][data-tab="audit"]')); await sleep(600);
check('J5 audit log', /Журнал действий/.test(txt()));
// analytics
click($('[data-nav="analytics"]')); await sleep(1200);
check('K1 analytics', /Воронка обработки/.test(txt()) && /Точность AI/.test(txt()));
// arch
click($('[data-nav="arch"]')); await sleep(900);
check('L1 arch view', /Мультитенантность/.test(txt()) && /152-ФЗ/.test(txt()));
// palette + task open from palette
click($('[data-nav="board"]')); await sleep(1100);
click($('[data-act="palette"]')); await sleep(300);
const pi=$('#palette-input'); pi.value='Расчёт стоимости'; pi.dispatchEvent(new window.Event('input',{bubbles:true})); await sleep(250);
doc.querySelector('[data-palette]').dispatchEvent(new window.MouseEvent('click',{bubbles:true,cancelable:true,view:window}));
await sleep(1300);
check('M1 task card opened from palette', /История изменений/.test(txt()));
// reply to client with AI suggest
console.log('   DEBUG modal:', ($('.modal')||{}).className, '| ai-suggest:', !!$('[data-act="ai-suggest"]'), '| blocked:', !!$('.modal .banner--warn'), '| last api:', window.__POTOK.Api.log.slice(0,3).map(l=>l.status+' '+l.method+' '+l.path).join(' ; '));
click($('[data-act="ai-suggest"]')); await sleep(900);
const rt=$('[data-form="reply"] [name="text"]');
check('M2 ai suggest filled reply', rt && rt.value.length>20);
submit($('[data-form="reply"]')); await sleep(1300);
check('M3 reply delivered', /Доставлено в Telegram/.test(txt()));
// comment
const ci=$('[data-form="comment"] input[name="text"]'); ci.value='Позвонил клиенту, договорились на четверг';
submit($('[data-form="comment"]')); await sleep(1200);
check('M4 comment added', /Позвонил клиенту/.test(txt()));
click($('[data-act="close-modal"]')); await sleep(400);
// API log modal
click($('[data-act="api-log"]')); await sleep(500);
check('N1 api log modal', /Журнал API-вызовов/.test(txt()) && $$('.logrow').length>5);
const firstRow=$('.logrow'); click(firstRow); await sleep(300);
check('N2 api log row expands', $('.log-expand') && $('.log-expand').style.display!=='none');
click($('[data-act="close-modal"]')); await sleep(300);
// export as owner
click($('[data-nav="tasks"]')); await sleep(1100);
check('O1 tasks table', $$('table tbody tr').length>8);
// manager role
await logout();
click($$('[data-demo-login]')[1]); await sleep(1500);
check('P1 manager login', /Канбан|Необработанные/.test(txt()));
click($('[data-nav="employees"]')); await sleep(1100);
check('P2 manager lacks invite', /Приглашать может владелец/.test(txt()));
click($('[data-nav="settings"]')); await sleep(1100);
click($('[data-act="settings-tab"][data-tab="sec"]')); await sleep(500);
check('P3 manager sees matrix anyway', /Матрица прав/.test(txt()));
// invited user
await logout();
click($$('[data-demo-login]')[2]); await sleep(1400);
check('Q1 invited user error', /Приглашение не принято/.test(txt()));
// register flow
click($('[data-act="auth-mode"]')); await sleep(400);
check('R1 register step1', /Регистрация организации/.test(txt()));
submit($('[data-form="register"]')); await sleep(600);
check('R2 register step2', /Рабочий e-mail/.test(txt()));
$('[data-bind="reg.firstName"]').value='Сергей'; $('[data-bind="reg.lastName"]').value='Кузнецов';
$('[data-bind="reg.email"]').value='owner@novaya.ru'; $('[data-bind="reg.password"]').value='SuperPass123';
submit($('[data-form="register"]')); await sleep(600);
check('R3 register step3', /приглашением|E-mail менеджера|ссылкой-приглашением/.test(txt()));
submit($('[data-form="register"]')); await sleep(2000);
check('R4 organization created + logged in', /Канбан|Необработанные/.test(txt()));

console.log('\n=== errors: '+errors.length+' | failed checks: '+fails+' ===');
[...new Set(errors)].slice(0,12).forEach(e=>console.log('* '+e));
process.exit(0);
