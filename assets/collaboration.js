
/* Finance Workbench collaboration surfaces. All authorization is enforced again by the API. */
(() => {
'use strict';
const labels={director:'财务总监',accountant:'会计',cashier:'出纳',reporter:'仓管／填报员',viewer:'只读',
 draft:'草稿',pending:'待处理',pending_reconcile:'待财务核对',submitted:'待审批',approved:'已批准',posted:'已过账',rejected:'已驳回',voided:'已作废',
 open:'待处理',done:'已处理',cancelled:'已撤销',resolved:'已处理',dismissed:'已排除',investigating:'核对中',
 queued:'排队中',running:'同步中',succeeded:'同步完成',partial:'完成，有异常',failed:'失败',confirmed_no_voucher:'已确认，无需凭证',superseded:'已被更正',
 warehouse:'仓管台账',expense:'报销台账',asset:'固定资产台账',cash:'资金登记',purchase_order:'采购单',receipt:'入库单',invoice:'发票',reimbursement:'报销单',asset_entry:'资产登记',cash_entry:'资金登记',
 unknown:'待确认',missing:'未开票',complete:'已开齐',not_required:'无需开票',advance:'预付款',shipment:'发货款',
 customer:'客户',supplier:'供应商',department:'部门',project:'研发项目',bank_account:'银行账户',
 changed:'疑似篡改',missing_candidate:'缺行待复核',duplicate_id:'来源 ID 重复',invalid_row:'字段异常',source_incomplete:'读取不完整',three_way_diff:'三单差异',field_diff:'字段差异',
 ledger:'台账审批',ledger_change:'台账更正',voucher:'凭证审批',reversal:'红冲申请',vat:'增值税结转',reconcile:'异常核对',invoice_followup:'催票',period_close:'锁定账期',period_unlock:'解锁账期'};
const L=v=>labels[v]||v||'—';
const invoiceLabel=v=>v==='partial'?'部分开票':L(v);
const state={access:null,prefs:null,ledgerModule:'warehouse',workflowView:'todo',offset:0,dimensions:[],connections:[],members:[],accounts:[],balanceCollapsed:new Set(),pageRequest:0};
let poll=null,shortcutVersion=0,inbox=null;
const E=(tag,text='',cls='')=>{const n=document.createElement(tag);if(text!==null)n.textContent=text;if(cls)n.className=cls;return n;};
const roles=()=>new Set(state.access?.roles||[]);
const finance=()=>!!state.access?.capabilities.financial_read;
const writeFinance=()=>!!state.access?.capabilities.financial_write;
const configure=()=>!!state.access?.capabilities.configure;
const qs=()=>'?account_set_id='+encodeURIComponent(SUITE_ACTIVE.id);
const requestKey=()=>crypto.randomUUID?crypto.randomUUID():Date.now().toString(36)+Math.random().toString(36).slice(2);
const api=(path,method='GET',body)=>{const i=path.indexOf('&'),resource=i<0?path:path.slice(0,i),query=i<0?'':path.slice(i);return apiRequest('/api/v2'+resource+qs()+query,{method,...(body===undefined?{}:{body:JSON.stringify(body)})});};
const cmd=(path,body={},method='POST')=>api(path,method,{requestKey:requestKey(),...body});
const customAccountKey='finance-standard:custom-accounts';
const customAccounts=()=>financialRead(customAccountKey.slice('finance-standard:'.length),[]);
function mergeCustomAccounts(accounts){
 for(const account of accounts){
  if(!account||SUBJ_MAP[account.code])continue;
  VOUCHER_ACCOUNTS.push(account);
  SUBJ_MAP[account.code]=account;
 }
 VOUCHER_ACCOUNTS.sort((a,b)=>a.code.localeCompare(b.code));
}
window.fwLoadCustomAccounts=()=>{
 if(storageMode!=='API')mergeCustomAccounts(customAccounts());
};
const accountContext=select=>{
 if(select.closest('dialog[data-custom-account]'))return null;
 if(select.id==='payroll-expense-account')return {prefixes:['6601','6602','5001'],base:'6602'};
 if(select.id==='inv-purchase-account')return {prefixes:['1401','1405'],base:select.value.slice(0,4)||'1405'};
 if(select.id==='inv-opening-account'||select.id==='fw-fx-code'||select.id==='acc-detail-account')return null;
 const label=select.closest('label')?.textContent.split(select.options[0]?.textContent||'\0')[0]||'';
 if(select.matches('.entry-account,#cost-debit,#cost-credit')||/科目/.test(label)||select.dataset.postingAccount==='true')return {};
 return null;
};
function refreshAccountPickers(){
 if(window.FW_V2_RESTRICTED||state.access&&!writeFinance())return;
 for(const select of document.querySelectorAll('select')){
  const context=accountContext(select);if(!context)continue;
  const existing=new Set(Array.from(select.options,option=>option.value));
  for(const account of VOUCHER_ACCOUNTS){
   if(account.isActive===false||existing.has(account.code))continue;
   if(context.prefixes&&!context.prefixes.some(prefix=>account.code.startsWith(prefix)))continue;
   if(context.prefixes||account.isCustom)select.add(new Option(account.code+' '+account.name,account.code));
  }
  if(select.nextElementSibling?.dataset.customAccountButton)continue;
  const add=btn('+ 自定义科目',()=>customAccountDialog(select));
  add.classList.add('fw-custom-account');add.dataset.customAccountButton='true';
  select.after(add);
 }
}
function customAccountDialog(picker){
 const context=accountContext(picker)||{}, modal=dialog('新增自定义科目');
 modal.d.dataset.customAccount='true';
 const grid=E('div','','fw2-grid');modal.content.append(grid);
 const name=field(grid,'name','科目名称');name.required=true;name.maxLength=120;
 const code=field(grid,'code','科目编码（可留空）');code.maxLength=10;
 const category=select(grid,'category','所属大类',['资产','负债','权益','成本','损益'].map(item=>[item,item]),context.base==='6602'?'损益':'资产');
 const direction=select(grid,'direction','余额方向',[['借','借'],['贷','贷']],'借');
 const selected=picker.value;
 const parentAccount=SUBJ_MAP[selected]||SUBJ_MAP[context.base];
 const parent=select(grid,'parent','自动编码的上级科目',[['','新增一级科目'],...VOUCHER_ACCOUNTS.filter(account=>account.code.length<10&&account.isActive!==false&&(!context.prefixes||context.prefixes.some(prefix=>account.code.startsWith(prefix)))).map(account=>[account.code,account.code+' '+account.name])],parentAccount?.code.length<10?parentAccount.code:context.base||'');
 const syncCategory=()=>{const inherited=SUBJ_MAP[parent.value];category.disabled=!!inherited;if(inherited){category.value=accountCategory(inherited);direction.value=inherited.dir||'借';}};
 parent.onchange=syncCategory;syncCategory();name.focus();
 submitForm(modal,'保存并选用',async()=>{
  const catalog=storageMode==='API'?(await api('/accounts')).items:VOUCHER_ACCOUNTS;
  const used=new Set(catalog.map(account=>account.code));
  const codeValue=code.value.trim()||nextCustomCode(used,parent.value);
  if(!/^[0-9]{4}(?:[0-9]{2}){0,3}$/.test(codeValue))throw Error('科目编码须为 4、6、8 或 10 位数字');
  if(used.has(codeValue))throw Error('该编码已存在，请更换编码或留空自动分配');
  if(context.prefixes&&!context.prefixes.some(prefix=>codeValue.startsWith(prefix)&&codeValue.length>prefix.length))throw Error('请在该业务允许的科目下新增明细');
  const parentCode=codeValue.length>4?codeValue.slice(0,-2):null;
  const parentRow=catalog.find(account=>account.code===parentCode);
  if(parentCode&&(!parentRow||parentRow.isActive===false||parentRow.is_active===0))throw Error('上级科目不存在或已停用');
  const record={code:codeValue,name:name.value.trim(),category:parentRow?accountCategory(parentRow):category.value,dir:direction.value,parentCode,opening:0,isActive:true,isCustom:true,aux:'none'};
  if(!record.name)throw Error('请输入科目名称');
  if(storageMode==='API'){
   await cmd('/accounts',{code:record.code,name:record.name,category:record.category,direction:record.dir});
   await apiLoadNormalizedReferenceData();
  }
  const saved=customAccounts().filter(account=>account.code!==record.code);
  localStorage.setItem(scopedKey(customAccountKey),JSON.stringify([...saved,record]));
  mergeCustomAccounts([record]);
  refreshAccountPickers();
  if(picker.isConnected){picker.value=record.code;picker.dispatchEvent(new Event('change',{bubbles:true}));}
  showToast('已新增科目 '+record.code+' '+record.name);
 });
}
function accountCategory(account){
 return account.category||({'1':'资产','2':'负债','4':'权益','5':'成本','6':'损益'}[account.code[0]]||'资产');
}
function nextCustomCode(used,parent){
 for(let suffix=1;suffix<=(parent?99:999);suffix++){
  const candidate=parent?parent+String(suffix).padStart(2,'0'):String(9000+suffix);
  if(!used.has(candidate))return candidate;
 }
 throw Error('此级编码已用完，请手动填写有效编码');
}
window.FinanceClient={access:()=>api('/access'),listLedgers:query=>api('/ledgers'+query),command:cmd,query:api};
function btn(text,fn,primary=false){const b=E('button',text,'btn'+(primary?' btn-primary':''));b.type='button';b.onclick=async()=>{b.disabled=true;try{await fn();}catch(e){showToast(e.message||'操作失败');}finally{if(b.isConnected)b.disabled=false;}};return b;}
function pill(value){const n=E('span',L(value),'fw2-pill');n.dataset.status=value;return n;}
function tools(parent){const n=E('div','','fw2-tools');parent.append(n);return n;}
function field(parent,name,label,type='text',value=''){const l=E('label',label);const n=E(type==='textarea'?'textarea':'input');if(type!=='textarea')n.type=type;n.name=name;n.value=value??'';if(type==='number')n.step='0.01';l.append(n);parent.append(l);return n;}
function select(parent,name,label,options,value=''){const l=E('label',label),n=E('select');n.name=name;for(const [v,t] of options){const o=E('option',t);o.value=v;n.append(o);}n.value=value;l.append(n);parent.append(l);return n;}
function check(parent,name,label,checked=false){const l=E('label','','fw2-check'),n=E('input');n.type='checkbox';n.name=name;n.checked=checked;l.append(n,E('span',label));parent.append(l);return n;}
function table(parent,headers,rows,empty='暂无记录。完成登记或同步后会在这里显示。'){
 const wrap=E('div','','fw2-table-wrap'),t=E('table','','fw2-table'),head=E('thead'),tr=E('tr');
 headers.forEach(h=>tr.append(E('th',h)));head.append(tr);t.append(head);const body=E('tbody');
 for(const cells of rows){const r=E('tr');cells.forEach((v,i)=>{const td=E('td');if(v instanceof Node)td.append(v);else td.textContent=v??'—';if(/金额|税额|借方|贷方|留抵|期初|期末/.test(headers[i]))td.classList.add('num');r.append(td);});body.append(r);}
 if(!rows.length){const r=E('tr'),td=E('td',empty,'fw2-empty');td.colSpan=headers.length;r.append(td);body.append(r);}t.append(body);wrap.append(t);parent.append(wrap);return t;
}
function actionGroup(...buttons){const n=E('div','','fw2-row-actions');buttons.filter(Boolean).forEach(b=>n.append(b));return n;}
function dialog(title){const d=E('dialog','','fw2-dialog'),h=E('h2',title),form=E('form'),content=E('div'),error=E('p','','fw2-error'),foot=E('div','','toolbar');h.id='fw2-dialog-'+requestKey();d.setAttribute('aria-labelledby',h.id);error.setAttribute('role','alert');form.append(content,error,foot);d.append(h,form);document.body.append(d);d.addEventListener('close',()=>d.remove(),{once:true});foot.append(btn('关闭',()=>d.close()));d.showModal();return {d,form,content,error,foot};}
function submitForm(modal,text,fn){const b=E('button',text,'btn btn-primary');b.type='submit';modal.foot.prepend(b);modal.form.onsubmit=async e=>{e.preventDefault();b.disabled=true;modal.error.textContent='';try{await fn(new FormData(modal.form));modal.d.close();}catch(err){modal.error.textContent=err.message;}finally{b.disabled=false;}};}
function page(id,title,description){const root=document.getElementById(id);root.replaceChildren();root.classList.add('fw2-page');const header=E('div','','fw2-heading'),text=E('div');text.append(E('h1',title),E('p',description));header.append(text);root.append(header);return {root,header};}
function pager(root,total,offset,refresh){const p=E('div','','fw2-pager');p.append(btn('上一页',()=>refresh(Math.max(offset-50,0))),E('span',total?`${offset+1}–${Math.min(offset+50,total)} / ${total}`:'0 条'),btn('下一页',()=>refresh(offset+50)));p.firstChild.disabled=offset===0;p.lastChild.disabled=offset+50>=total;root.append(p);}
function busy(root,text='正在读取…'){const p=E('p',text,'fw2-status');p.setAttribute('role','status');root.append(p);return p;}
function ensure(){if(!state.access)throw Error('请先连接服务并登录。可使用右上角“账户”启用多人协作。');}
// 新建独立一级导航组，不移动任何既有 leaf key（旧收藏与跨模块跳转继续有效）。
NAV_TREE.push({id:'collab',num:String(NAV_TREE.length+1),title:'协作与同步',children:[]});
const definitions=[
  ['collab','collab-hub','协作与同步总览','module-collab-hub'],
  ['biz','collab-ledgers','仓管登记与业务台账','module-collab-ledgers'],
  ['collab','collab-sync','外部表格与同步','module-collab-sync'],
 ['fund','collab-expenses','报销登记与审批','module-collab-expenses'],
 ['fa','collab-assets','资产填报台账','module-collab-assets'],
 ['gl','collab-accounts','科目与辅助核算','module-collab-accounts'],
 ['gl','collab-vouchers','凭证审批与过账','module-collab-vouchers'],
 ['gl','collab-alerts','同步异常与三单核对','module-collab-alerts'],
 ['gl','collab-audit','操作审计日志','module-collab-audit'],
 ['collab','collab-settings','协作成员与权限','module-collab-settings'],
 ['gl','collab-periods','账期锁定与解锁','module-collab-periods'],
 ['tax','tax-vat-close','进项与销项期末结转','module-collab-vat']
];
for(const [group,key,label,moduleId] of definitions){NAV_TREE.find(g=>g.id===group).children.push({key,label,moduleId});}
const taxOrder=['tax-calc','tax-input-inv','tax-vat-close','tax-overview','tax-burden','tax-history','finance-guide','tax-wizard','tax-calendar'];
const taxItems=NAV_TREE.find(group=>group.id==='tax').children;
taxItems.push({key:'finance-guide',label:'财务工作全流程指导',moduleId:'module-finance-guide'});
taxItems.sort((a,b)=>taxOrder.indexOf(a.key)-taxOrder.indexOf(b.key));
for(const item of taxItems){
 item.section=taxOrder.indexOf(item.key)<6?'税务计算与申报':'业务引导与日历';
 if(item.key==='tax-history')item.label='历史记录与对比';
}
EXTRA_TABS['workflow-center']={key:'workflow-center',label:'流程中心',moduleId:'module-collab-workflow'};
for(const id of [...definitions.map(d=>d[3]),'module-collab-workflow','module-collab-balance','module-finance-guide']){const n=E('section','','module');n.id=id;n.style.display='none';document.querySelector('.workspace-content').append(n);}
const allNav=NAV_TREE.map(g=>({...g,children:[...g.children]}));
function allowed(key){
 if(!state.access)return true;
 if(['home','workflow-center','finance-guide'].includes(key))return true;
 if(key==='collab-settings')return configure();
  if(key==='collab-hub')return configure()||finance();
 if(finance())return true;
 if(key==='collab-ledgers')return roles().has('cashier')||state.access.capabilities.scopes.includes('warehouse');
 if(key==='collab-expenses')return state.access.capabilities.scopes.includes('expense');
 if(key==='collab-assets')return state.access.capabilities.scopes.includes('asset');
 return key==='fund-bank'&&roles().has('cashier');
}
function applyNavigation(){
 for(const g of NAV_TREE){const full=allNav.find(x=>x.id===g.id);g.children=full.children.filter(x=>allowed(x.key));}
 renderSidebarTree();document.querySelectorAll('.tree-group').forEach(n=>{n.hidden=!NAV_TREE.find(g=>g.id===n.dataset.groupId).children.length;});
 if(finance()){const balance=NAV_TREE.flatMap(g=>g.children).find(x=>x.key==='gl-balance');if(balance)balance.moduleId='module-collab-balance';}
}
const originalActivate=activateWorkspaceTab;
activateWorkspaceTab=function(key){if(!allowed(key)){showToast('当前账号无此模块权限');return;}originalActivate(key);const module=findNavMeta(key)?.moduleId;
 const renderers={'module-collab-hub':renderCollabHub,'module-collab-ledgers':()=>renderLedgers('warehouse'),'module-collab-expenses':()=>renderLedgers('expense'),'module-collab-assets':()=>renderLedgers('asset'),
  'module-collab-sync':renderSync,'module-collab-accounts':renderAccounts,'module-collab-vouchers':renderVouchers,'module-collab-alerts':renderAlerts,'module-collab-audit':renderAudit,'module-collab-settings':renderSettings,'module-collab-periods':renderPeriods,'module-collab-vat':renderVAT,'module-collab-workflow':()=>renderWorkflow(0),'module-collab-balance':renderBalance};
 if(renderers[module])void runAction(async()=>{ensure();await renderers[module]();});
 if(module==='module-finance-guide')renderGuide();
 if(key==='home'&&document.querySelector('.fw-home-metrics'))void renderHomeMetrics(document.querySelector('#module-home .home-container'));
};
function renderGuide(){
 const {root}=page('module-finance-guide','财务工作全流程指导','按月度做账顺序展开，从核对原始单据到出具报表。');
 const stages=[
  ['期初建账与初始化','选择适用的会计准则和启用年月。迁入期初时先做借贷试算，再核对银行、往来、库存辅助明细与总账。固定资产卡片应录入原值、累计折旧、使用年限和启用日期。',
   [['企业建账',()=>fwWizardDialog(),!state.access||configure()],['期初余额','fin-summary'],['期初库存','biz-inventory'],['固定资产卡片','fa-card']]],
  ['日常业务与资金核算','依据采购、销售和报销单据入账，核对数量、金额与发票。收付款后按客户或供应商核销往来。月内定期对照银行流水，记录未达账项及差异原因。',
   [['采购入库／销售出库','biz-inventory'],['费用报销','collab-expenses','fund-expense'],['往来核销（AR/AP）','ar-subledger'],['收付款登记','fund-bank'],['银行流水对账','fund-loan']]],
  ['税务申报与期末结转','核对进项发票的用途确认及抵扣状态，汇总销项并核对总账。按适用税种完成测算与申报复核，再办理增值税结转、折旧计提和损益结转。',
   [['进项发票管理','tax-input-inv'],['销项发票与开票','invoice'],['七大税种测算','tax-calc'],['进销项期末结转','tax-vat-close'],['月度折旧','fa-depr'],['期末损益结转','fin-closing'],['申报日历','tax-calendar']]],
  ['出具财务报表','完成审批记账和期末核对后，从同一账套和期间生成三张报表。核对资产等于负债加所有者权益、利润与结转一致、期末现金与资金余额勾稽。',
   [['资产负债表','fin-bs'],['利润表','fin-pl'],['现金流量表','fin-cf']]]
 ];
 for(const [title,description,actions] of stages){
  const section=E('section','','fw-guide-stage'),links=E('div','','toolbar');
  section.append(E('h2',title),E('p',description),links);
  for(const [label,target,alternative] of actions){
   if(typeof target==='function'){if(alternative)links.append(btn(label,target));continue;}
   const route=!state.access&&typeof alternative==='string'?alternative:target;
   if(allowed(route))links.append(btn(label,()=>openWorkspaceTab(route)));
  }
  root.append(section);
 }
}
async function renderHomeMetrics(home){
 let panel=home.querySelector('.fw-home-metrics');
 if(!panel){panel=E('section','','fw-home-metrics');home.querySelector('.home-header-text').after(panel);}
 panel.replaceChildren();
 if(state.access&&!finance())return;
 const period=financialRead('period',localDay().slice(0,7)), list=E('dl');
 for(const label of ['货币资金余额','本期营业收入','本期净利润','已记账凭证']){
  const cell=E('div');cell.append(E('dt',label),E('dd','—'));list.append(cell);
 }
 panel.append(E('h2','财务概览 · '+period),list);
 try{
  const book=await financialBook(period), report=profitStatement(book,period);
  const cash=book.rows.filter(row=>/^(1001|1002|1012)/.test(row.code)).reduce((sum,row)=>sum+row.end,0);
  const revenue=-book.rows.filter(row=>/^(6001|6051)/.test(row.code)).reduce((sum,row)=>sum+row.debit-row.credit,0);
  const profit=report.rows.find(row=>row[1]==='四、净利润')?.[2]||0;
  const values=[fromCents(cash),fromCents(revenue||0),fromCents(profit),book.vouchers.filter(row=>row.period===period).length+' 张'];
  list.querySelectorAll('dd').forEach((item,i)=>item.textContent=values[i]);
 }catch{list.setAttribute('aria-label','暂时无法读取本期财务数据');}
}
async function loadDimensions(){state.dimensions=(await api('/dimensions')).items;return state.dimensions;}
function dimensionSelect(parent,key,kind,value){return select(parent,key,L(kind),[['','未指定'],...state.dimensions.filter(d=>d.kind===kind).map(d=>[d.id,d.name])],value||'');}
async function renderHome(){
 const home=document.querySelector('#module-home .home-container'),cards=home.querySelector('.home-cards');
 home.querySelector('.home-title').textContent='财务工作台';home.querySelector('.home-desc').textContent=state.access?`${activeSuiteBook().name} · ${state.access.name} · ${state.access.roles.map(L).join('、')}`:'从常用入口开始，处理登记、核对、审批与月末工作。';
 let toolbar=home.querySelector('.fw2-home-toolbar');if(!toolbar){toolbar=E('div','','fw2-home-toolbar');toolbar.append(E('h2','常用模块'),btn('编辑常用模块',editShortcuts));cards.before(toolbar);}
 const choices=shortcutChoices();let chosen=state.prefs?.shortcuts;
 if(chosen==null)chosen=allNav.filter(g=>g.children.some(x=>allowed(x.key))).map(g=>'group-'+g.id);
 cards.replaceChildren();for(const key of chosen){const entry=choices.find(x=>x.key===key);if(!entry)continue;const card=E('button','','home-card');card.type='button';card.dataset.shortcut=key;card.append(E('h3',entry.label),E('p',entry.description));card.onclick=()=>key.startsWith('group-')?revealSidebarGroup(key.slice(6)):openWorkspaceTab(key);cards.append(card);}
 if(!cards.childElementCount)cards.append(E('p','尚未选择常用模块。点击“编辑常用模块”添加。','fw2-empty'));
 void renderHomeMetrics(home);
}
function shortcutChoices(){return allNav.flatMap(g=>g.children.some(x=>allowed(x.key))?[{key:'group-'+g.id,label:g.title,description:'展开此模块的功能入口',group:g.title},...g.children.filter(x=>allowed(x.key)).map(x=>({key:x.key,label:x.label,description:g.title,group:g.title}))]:[]);}
function editShortcuts(){
 const m=dialog('编辑常用模块'),choices=shortcutChoices(),chosen=[...(state.prefs?.shortcuts??choices.filter(x=>x.key.startsWith('group-')).map(x=>x.key))].filter(k=>choices.some(x=>x.key===k));
 m.content.append(E('p','勾选常用模块与子功能，在下方拖拽排序，也可使用上移／下移按钮。','fw2-note'));const list=E('div','','fw2-select-list'),order=E('div');m.content.append(list,E('h3','显示顺序'),order);
 const redraw=()=>{order.replaceChildren();chosen.forEach((key,i)=>{const r=E('div','','fw2-sort-row');r.draggable=true;r.append(E('span',choices.find(c=>c.key===key)?.label),btn('上移',()=>{if(i){[chosen[i-1],chosen[i]]=[chosen[i],chosen[i-1]];redraw();}}),btn('下移',()=>{if(i<chosen.length-1){[chosen[i+1],chosen[i]]=[chosen[i],chosen[i+1]];redraw();}}));r.ondragstart=e=>{e.dataTransfer.setData('text/plain',key);r.dataset.dragging='true';};r.ondragend=()=>delete r.dataset.dragging;r.ondragover=e=>e.preventDefault();r.ondrop=e=>{e.preventDefault();const from=chosen.indexOf(e.dataTransfer.getData('text/plain'));if(from<0)return;const [v]=chosen.splice(from,1);chosen.splice(i,0,v);redraw();};order.append(r);});};
 for(const group of allNav){const options=choices.filter(c=>c.group===group.title);if(!options.length)continue;const fs=E('fieldset');fs.append(E('legend',group.title));for(const item of options){const input=check(fs,item.key,item.label,chosen.includes(item.key));input.onchange=()=>{if(input.checked){if(chosen.length>=30){input.checked=false;showToast('最多选择 30 个入口');return;}chosen.push(item.key);}else chosen.splice(chosen.indexOf(item.key),1);redraw();};}list.append(fs);}redraw();
 submitForm(m,'保存常用模块',async()=>{if(state.access){const saved=await cmd('/preferences',{shortcuts:chosen,expectedVersion:shortcutVersion},'PUT');shortcutVersion=saved.version;state.prefs=saved;}else{state.prefs={shortcuts:chosen};localStorage.setItem('fw-demo-shortcuts:'+SUITE_ACTIVE.id,JSON.stringify(state.prefs));}await renderHome();showToast('常用模块已保存');});
}
async function refreshBell(){if(!state.access)return;const summary=await api('/workflow/summary'),badge=document.getElementById('fw2-badge');badge.hidden=!summary.badge;badge.textContent=summary.badge>99?'99+':summary.badge;badge.parentElement.setAttribute('aria-label',`待办提醒：${summary.todo} 项待办，${summary.unread} 条未读`);}
async function toggleInbox(){if(inbox){inbox.remove();inbox=null;return;}ensure();inbox=E('div','','fw2-inbox');inbox.setAttribute('role','region');inbox.setAttribute('aria-label','待办提醒');document.body.append(inbox);const loading=busy(inbox);try{const [s,t,n]=await Promise.all([api('/workflow/summary'),api('/workflow/items&view=todo&limit=5'),api('/workflow/items&view=unread&limit=5')]);loading.remove();inbox.append(E('h3','待办提醒'),E('p',`${s.todo} 项待办 · ${s.unread} 条未读`,'fw2-status'));for(const item of [...t.items,...n.items]){const b=E('button',item.title,'fw2-inbox-row');b.type='button';b.onclick=async()=>{inbox?.remove();inbox=null;openWorkspaceTab('workflow-center');await taskDialog(item);};inbox.append(b);}if(!t.items.length&&!n.items.length)inbox.append(E('p','当前账套的待办已处理完毕。','fw2-empty'));inbox.append(btn('打开流程中心',()=>{inbox?.remove();inbox=null;openWorkspaceTab('workflow-center');}));}catch(e){loading.textContent=e.message;}}
async function renderWorkflow(offset=0){
 const {root}=page('module-collab-workflow','流程中心','处理当前账套中指派给你的审批、核对与催票任务，查看通知和申请进度。'),tabs=E('div','','fw2-tabs');root.append(tabs);
 for(const [v,t] of [['todo','我的待办'],['unread','我的待阅'],['done','我的已办'],['read','我的已阅'],['initiated','我的发起']]){const b=btn(t,()=>{state.workflowView=v;return renderWorkflow(0);});b.setAttribute('aria-selected',String(v===state.workflowView));tabs.append(b);}
 const bar=tools(root),category=select(bar,'category','任务分类',[['','全部'],...['voucher','reimbursement','ledger','ledger_change','reversal','reconcile','invoice_followup','vat'].map(x=>[x,L(x)])],state.workflowCategory||''),initiator=field(bar,'initiator','发起人','text',state.workflowInitiator||''),status=select(bar,'status','流程状态',[['','全部'],['pending','待处理'],['approved','已批准'],['rejected','已驳回'],['resolved','已处理'],['cancelled','已撤销']],state.workflowStatus||'');
 bar.append(btn('筛选',()=>{state.workflowCategory=category.value;state.workflowInitiator=initiator.value;state.workflowStatus=status.value;return renderWorkflow(0);}));const load=busy(root);
 const result=await api('/workflow/items&view='+state.workflowView+'&category='+encodeURIComponent(state.workflowCategory||'')+'&initiator='+encodeURIComponent(state.workflowInitiator||'')+'&status='+encodeURIComponent(state.workflowStatus||'')+'&limit=50&offset='+offset);load.remove();
 table(root,['任务','分类','发起人','状态','发起时间','操作'],result.items.map(r=>[r.title,L(r.category),r.initiated_by,pill(r.case_state),r.created_at,btn('查看与处理',()=>taskDialog(r))]),'此视图暂无任务。');pager(root,result.total,offset,renderWorkflow);await refreshBell();
}
function showSubject(parent,subject){if(!subject)return;const names={doc_no:'单据号',business_date:'业务日期',kind:'单据类型',amount:'含税金额',tax_amount:'税额',quantity:'数量',payment_kind:'款项性质',invoice_state:'原登记发票状态',effective_invoice_state:'当前发票状态',reason:'更正原因',voucher_no:'凭证号',voucher_date:'凭证日期',summary:'摘要',created_by:'制单人',reviewed_by:'终审人',status:'状态'};const dl=E('dl','','fw2-detail');for(const [key,name] of Object.entries(names)){if(subject[key]==null)continue;dl.append(E('dt',name),E('dd',L(subject[key])));}parent.append(dl);if(subject.entries)table(parent,['科目','摘要','借方金额','贷方金额'],subject.entries.map(e=>[`${e.account_code} ${e.account_name||''}`,e.summary,e.debit_amount,e.credit_amount]));}
async function taskDialog(task){
 const m=dialog(task.title||'流程详情'),load=busy(m.content),detail=await api('/workflow/cases/'+task.case_id);load.remove();m.content.append(E('p',`${L(detail.category)} · ${L(detail.state)} · 发起人 ${detail.initiated_by}`,'fw2-status'));showSubject(m.content,detail.subject);
 if(detail.payload?.reason)m.content.append(E('p',detail.payload.reason));
 if(detail.category==='vat')m.content.append(btn('查看结转单',()=>{m.d.close();openWorkspaceTab('tax-vat-close');}));
 if(['period_close','period_unlock'].includes(detail.category)){m.content.append(E('p','账期：'+detail.subject_id));m.content.append(btn('查看账期检查',()=>{m.d.close();openWorkspaceTab('collab-periods');}));}
 if(detail.category==='reconcile')m.content.append(btn('查看差异证据',()=>{m.d.close();openWorkspaceTab('collab-alerts');}));
 if(task.kind==='notice'&&!task.read_at){await cmd('/workflow/tasks/'+task.id+'/read');await refreshBell();}
 if(task.kind==='action'&&task.state==='open'&&detail.state==='pending'&&detail.category==='invoice_followup'&&detail.subject)m.foot.prepend(btn('更新催票状态',()=>{m.d.close();return followupDialog(detail.subject);}));
 if(task.kind==='action'&&task.state==='open'&&detail.state==='pending'&&detail.category!=='invoice_followup'){
  const note=field(m.content,'note','处理说明（驳回必填）','textarea');
  for(const [decision,title] of (detail.category==='reconcile'?[['resolve','确认处理完成']]:[['approve',task.step===1&&detail.category==='reimbursement'?'初审通过':'批准'],['reject','驳回']]))m.foot.prepend(btn(title,async()=>{try{await cmd('/workflow/tasks/'+task.id+'/actions',{decision,note:note.value});m.d.close();await renderWorkflow(0);showToast('处理结果已保存');}catch(e){m.error.textContent=e.message;}},decision!=='reject'));
 }
 if(detail.initiated_by===state.access.name&&detail.state==='pending'&&['voucher','ledger','ledger_change','reimbursement','reversal'].includes(detail.category))m.foot.prepend(btn('撤回申请',async()=>{await cmd('/workflow/cases/'+detail.id+'/cancel',{reason:'发起人撤回申请'});m.d.close();await renderWorkflow(0);}));
}

async function renderLedgers(module=state.ledgerModule,offset=0){
 state.ledgerModule=module;const moduleId=module==='expense'?'module-collab-expenses':module==='asset'?'module-collab-assets':'module-collab-ledgers';
 const {root,header}=page(moduleId,L(module),'来源入账后保留原始版本。财务更正、审批、制证和催票均在系统中留痕。');
 if(writeFinance()||roles().has('reporter')&&state.access.capabilities.scopes.includes(module))header.append(btn('新增登记',()=>ledgerDialog(null,module),true));
 const bar=tools(root);bar.append(btn('刷新',()=>renderLedgers(module,offset)));if(finance())bar.append(btn('查看同步异常',()=>openWorkspaceTab('collab-alerts')));
 const load=busy(root),result=await api('/ledgers&module='+module+'&limit=50&offset='+offset);load.remove();
 table(root,['业务日期','单据／类型','含税金额','数量','款项性质','发票状态','提交人','状态','操作'],result.items.map(r=>[r.business_date,`${r.doc_no} / ${L(r.kind)}`,r.amount,r.quantity,L(r.payment_kind),invoiceLabel(r.effective_invoice_state||r.invoice_state),r.owner_verified?r.created_by:'外部导入（归属待核对）',pill(r.status),actionGroup(
 btn('查看',()=>ledgerDetail(r)),
 !r.is_locked&&['draft','rejected'].includes(r.status)&&(writeFinance()||r.created_by===state.access.name)?btn('编辑',()=>ledgerDialog(r,module)):null,
 ['draft','rejected','pending_reconcile'].includes(r.status)&&(writeFinance()||!r.source_row_id&&r.created_by===state.access.name)?btn('提交',async()=>{await cmd('/ledgers/'+r.id+'/submit',{expectedVersion:r.version});await renderLedgers(module,offset);await refreshBell();}):null,
 r.is_locked&&writeFinance()?btn('更正',()=>ledgerDialog(r,module,true)):null,
 r.status==='approved'&&writeFinance()&&!r.voucher_id?btn('生成凭证',()=>ledgerVoucherDialog(r)):null,
 (writeFinance()||roles().has('cashier'))?btn('催票',()=>followupDialog(r)):null
 )]));pager(root,result.total,offset,o=>renderLedgers(module,o));
}
async function ledgerDetail(r){await loadDimensions();const m=dialog(r.doc_no+' · '+L(r.kind));showSubject(m.content,r);for(const [key,title] of [['project_id','研发项目'],['supplier_id','供应商'],['department_id','部门']])if(r[key])m.content.append(E('p',title+'：'+(state.dimensions.find(d=>d.id===r[key])?.name||r[key])));if(r.is_locked)m.content.append(E('p','此版本已锁定，修改需由财务发起更正。','fw2-note'));if(finance())m.foot.prepend(btn('查看变更记录',async()=>{m.d.close();await auditDialog(r.id,'fw_ledger_items');}));}
async function ledgerDialog(record,module,correction=false){
 await loadDimensions();const m=dialog(correction?'发起台账更正':record?'编辑登记':'新增'+L(module)),g=E('div','','fw2-grid');m.content.append(g);
 const kinds={warehouse:['purchase_order','receipt','invoice'],expense:['reimbursement','invoice'],asset:['asset_entry','invoice'],cash:['cash_entry']};
 select(g,'kind','单据类型',kinds[module].map(v=>[v,L(v)]),record?.kind||kinds[module][0]);field(g,'doc_no','单据号','text',record?.doc_no);field(g,'business_date','业务日期','date',record?.business_date||new Date().toLocaleDateString('sv-SE'));
 field(g,'amount','含税金额（元）','number',record?.amount||'0.00');field(g,'tax_amount','税额（元）','number',record?.tax_amount||'0.00');const q=field(g,'quantity','数量','number',record?.quantity||'0');q.step='0.000001';field(g,'item_code','商品／资产编码','text',record?.item_code);
 select(g,'payment_kind','款项性质',[['','未指定'],['advance','预付款'],['shipment','发货款']],record?.payment_kind||'');select(g,'invoice_state','票是否开齐',['unknown','missing','partial','complete','not_required'].map(v=>[v,L(v)]),record?.invoice_state||'unknown');
 dimensionSelect(g,'supplier_id','supplier',record?.supplier_id);dimensionSelect(g,'project_id','project',record?.project_id);dimensionSelect(g,'department_id','department',record?.department_id);
 const follow=check(g,'needs_invoice_followup','是否需要催票',!!record?.needs_invoice_followup),visible=writeFinance()?check(g,'cashier_visible','相关台账对出纳可见',!!record?.cashier_visible):null;
 if(correction)field(g,'reason','更正原因','textarea').parentElement.classList.add('fw2-wide');
 submitForm(m,correction?'保存更正草稿':'保存登记',async data=>{const p=Object.fromEntries(data);p.module=module;p.needs_invoice_followup=follow.checked;p.cashier_visible=visible?visible.checked:!!record?.cashier_visible;if(record)p.expectedVersion=record.version;await cmd('/ledgers'+(record?'/'+record.id+(correction?'/changes':''):''),p,record&&!correction?'PATCH':'POST');await renderLedgers(module);showToast(correction?'更正草稿已保存，请提交审批':'登记已保存');});
}
async function ledgerVoucherDialog(r){
 const m=dialog('从台账生成凭证草稿');m.content.append(E('p',`${r.doc_no} · 含税金额 ${r.amount} 元`,'fw2-note'));const accounts=(await api('/accounts')).items.filter(x=>x.is_active),options=accounts.map(a=>[a.code,a.code+' '+a.name]);
 const debit=select(m.content,'debit','借方科目',options,r.module==='asset'?'1601':r.module==='expense'?'6602':'1405'),credit=select(m.content,'credit','贷方科目',options,'2202');debit.dataset.postingAccount=credit.dataset.postingAccount='true';refreshAccountPickers();const tax=check(m.content,'deduct','税额按可抵扣进项单列',r.tax_cents>0);const day=field(m.content,'date','凭证日期','date',r.business_date);
 submitForm(m,'生成草稿',async()=>{const dims={...(r.project_id?{project:r.project_id}:{}),...(r.department_id?{department:r.department_id}:{})};const amount=(r.amount_cents-(tax.checked?r.tax_cents:0))/100;const entries=[{account_code:debit.value,debit_amount:amount.toFixed(2),credit_amount:'0.00',dimensions:dims},{account_code:credit.value,debit_amount:'0.00',credit_amount:r.amount}];if(tax.checked&&r.tax_cents)entries.push({account_code:'22210101',debit_amount:r.tax_amount,credit_amount:'0.00'});await cmd('/ledgers/'+r.id+'/voucher',{expectedVersion:r.version,voucher_date:day.value,entries});await renderLedgers(r.module);showToast('凭证草稿已生成，请在“凭证审批与过账”提交');});
}
function followupDialog(r){const m=dialog('更新催票状态'),stateInput=select(m.content,'state','催票进度',[['pending','待催票'],['reminded','已催票'],['received','已收票'],['closed','已结束']],'reminded'),invoice=select(m.content,'invoice_state','当前开票情况',[['missing','未开票'],['partial','部分开票'],['complete','已开齐'],['not_required','无需开票']],r.effective_invoice_state||r.invoice_state),note=field(m.content,'note','处理说明','textarea');submitForm(m,'保存催票记录',async()=>{await cmd('/ledgers/'+r.id+'/followups',{state:stateInput.value,invoice_state:invoice.value,note:note.value});await renderLedgers(r.module);});}

// 共用同一份 state.connections、表格与按钮：独立页和总览页都使用本函数。
async function renderSyncContent(root){
 const load=busy(root),result=await api('/sync/connections');load.remove();
 state.connections=Array.isArray(result.items)?result.items:[];
 table(root,['连接','来源／业务','最近成功读取','最近任务','新增／成功／异常','操作'],state.connections.map(r=>{
  const stats=r.last_run?.stats||{};
  const syncButton=writeFinance()?btn('立即同步',async()=>{
   const run=await cmd('/sync/connections/'+encodeURIComponent(r.id)+'/runs');
   showToast(run.reused?'已有同步任务，正在读取进度':'同步任务已提交');
   await refreshCollabSurface('sync');
   setTimeout(()=>{if(['collab-hub','collab-sync'].includes(activeTabKey))void runAction(()=>refreshCollabSurface('sync'));},2000);
  }):null;
  const unlinkButton=configure()?btn('删除／解绑',()=>unlinkConnectionDialog(r)):null;
  if(unlinkButton)unlinkButton.classList.add('fw2-danger-button');
  return [r.name,(r.provider==='feishu'?'飞书多维表格':'Google Sheets')+' / '+L(r.module),
   r.last_success_at||'尚未同步',pill(r.last_run?.status||'draft'),
   `${stats.new||0} / ${stats.success||0} / ${stats.anomalies||0}`,
   actionGroup(syncButton,configure()?btn('编辑',()=>connectionDialog(r)):null,
    configure()?btn('配置凭据',()=>credentialDialog(r)):null,
    configure()&&r.provider==='feishu'?btn('绑定填报人',()=>bindingDialog(r)):null,
    r.last_run?btn('查看结果',()=>runDialog(r.last_run)):null,unlinkButton)];
 }), '尚未连接表格。财务总监可新增连接并配置字段映射。');
 root.append(E('p','解绑只删除同步连接配置，不删除已有账套、原始凭证或已保存的业务台账。历史审计记录由服务端保留。','fw2-note'));
}
async function renderSync(){
 const {root,header}=page('module-collab-sync','外部表格与定时同步',
  '完整读取并核验历史，只将新增行登记为锁定台账。每天北京时间 02:17 自动同步，服务停机后启动时补跑当日任务。');
 if(configure())header.append(btn('新增表格连接',()=>connectionDialog(),true));
 await renderSyncContent(root);
}
function unlinkConnectionDialog(r){
 if(!configure())throw Error('当前账号没有解绑连接的权限');
 const m=dialog('确认删除／解绑外部表格连接');
 const note=E('p','','fw2-destructive-notice');
 note.append('即将解绑连接「',E('strong',r.name),'」。只会解除外部表格同步连接并停止后续同步；',
  E('strong','不会删除本地或服务端已经保存的账套数据、凭证与台账。'));
 m.content.append(note,E('p','已完成的同步任务及审计历史按服务端保留策略处理。','fw2-note'));
 submitForm(m,'确认解绑',async()=>{
  // 连接配置仅以 /api/v2/sync/connections 为权威来源，不存在本地配置副本或缓存 Key。
  await api('/sync/connections/'+encodeURIComponent(r.id),'DELETE',
   {requestKey:requestKey(),expectedVersion:r.version});
  state.connections=state.connections.filter(item=>String(item.id)!==String(r.id));
  await refreshCollabSurface('sync');showToast('已解绑外部表格，本地账套数据未受影响');
 });
 m.foot.querySelector('[type=submit]').classList.add('fw2-danger-submit');
}

function connectionDialog(r){
 const m=dialog(r?'编辑表格连接':'新增表格连接'),g=E('div','','fw2-grid');m.content.append(g);
 field(g,'name','连接名称','text',r?.name);const provider=select(g,'provider','来源',[['feishu','飞书多维表格'],['google_sheets','Google Sheets']],r?.provider||'feishu');select(g,'module','业务用途',[['warehouse','仓管台账'],['expense','报销台账'],['asset','固定资产台账']],r?.module||'warehouse');
 field(g,'source_ref','飞书 app_token／Google spreadsheetId','text',r?.source_ref);field(g,'table_ref','飞书 table_id／Google 工作表名称','text',r?.table_ref);field(g,'secret_ref','服务端凭据名称（大写英文）','text',r?.secret_ref||'FINANCE_SOURCE');const enabled=check(g,'enabled','启用此连接',r?!!r.enabled:true),scheduled=check(g,'scheduled','每天凌晨自动同步',!!r?.scheduled);
 m.content.append(E('h3','字段映射'),E('p','填写来源表中的列名。日期使用 YYYY-MM-DD；Google 表必须提供稳定 external_id 列。','fw2-note'));const mapGrid=E('div','','fw2-grid');m.content.append(mapGrid);
 const mappings={external_id:'Google 稳定 ID',business_date:'业务日期',amount:'含税金额',tax_amount:'税额',doc_no:'单据号',doc_line:'行号',kind:'单据类型',quantity:'数量',item_code:'商品编码',payment_kind:'款项性质',invoice_state:'票是否开齐',needs_invoice_followup:'是否需要催票',project_id:'研发项目（编码或名称）',supplier_id:'供应商（编码或名称）',department_id:'部门（编码或名称）'};
 const defaultMap={external_id:'external_id',business_date:'业务日期',amount:'金额',tax_amount:'税额',doc_no:'单据号',quantity:'数量',payment_kind:'款项性质',invoice_state:'票是否开齐',needs_invoice_followup:'是否需要催票'};
 const inputs={};for(const [key,label] of Object.entries(mappings))inputs[key]=field(mapGrid,key,label,'text',(r?.mapping||defaultMap)[key]||'');
 if(r){provider.disabled=true;for(const name of ['module','source_ref','table_ref'])g.querySelector(`[name="${name}"]`).disabled=true;}
 submitForm(m,'保存连接',async()=>{const data=Object.fromEntries(new FormData(m.form));if(r)Object.assign(data,{id:r.id,provider:r.provider,module:r.module,source_ref:r.source_ref,table_ref:r.table_ref,expectedVersion:r.version});data.enabled=enabled.checked;data.scheduled=scheduled.checked;data.mapping=Object.fromEntries(Object.entries(inputs).filter(([,n])=>n.value.trim()).map(([k,n])=>[k,n.value.trim()]));for(const key of Object.keys(mappings))delete data[key];await cmd('/sync/connections',data);await refreshCollabSurface('sync');showToast('表格连接已保存');});
}
function credentialDialog(r){const m=dialog('配置服务端凭据');m.content.append(E('p','凭据加密保存，仅可写入和替换；页面不会读回密钥。','fw2-note'));let credentials;
 if(r.provider==='feishu'){const id=field(m.content,'app_id','飞书 App ID'),secret=field(m.content,'app_secret','飞书 App Secret','password');credentials=()=>({app_id:id.value,app_secret:secret.value});}
 else{const json=field(m.content,'json','Google 服务账号 JSON','textarea');json.autocomplete='off';credentials=()=>{try{return JSON.parse(json.value);}catch{throw Error('服务账号 JSON 格式无效');}};m.content.append(E('p','请将目标 Google 表格共享给服务账号邮箱，并授予读取权限。','fw2-note'));}
 submitForm(m,'加密保存凭据',async()=>{await cmd('/sync/credentials',{secret_ref:r.secret_ref,credentials:credentials()},'PUT');showToast('凭据已保存，可点击立即同步验证');});}
async function bindingDialog(r){const m=dialog('绑定飞书来源填报人'),principal=field(m.content,'external_principal','飞书返回的创建者 ID'),name=field(m.content,'user_name','本系统成员账号');m.content.append(E('p','仅绑定官方 API 提供的创建者标识。无法验证归属的外部行只向财务开放。','fw2-note'));submitForm(m,'保存绑定',()=>cmd('/sync/identities',{connection_id:r.id,external_principal:principal.value,user_name:name.value}));}
function runDialog(r){const m=dialog('同步任务结果');m.content.append(E('p',`${L(r.status)} · ${r.started_at}`,'fw2-status'));table(m.content,['指标','数量'],Object.entries(r.stats||{}).map(([k,v])=>[({read:'读取行数',new:'新增',success:'入账成功',anomalies:'异常',failed:'失败',unchanged:'未变更',duplicate:'重复 ID'})[k]||k,String(v)]));if(r.error_summary)m.content.append(E('p',r.error_summary,'fw2-error'));m.foot.prepend(btn('打开异常核对',()=>{m.d.close();openWorkspaceTab('collab-alerts');}));}

async function renderAlerts(){
 const {root,header}=page('module-collab-alerts','同步异常与三单核对','核查外部已入账行的改动、缺失和三单差异。处理预警不会覆盖原始账目。');if(writeFinance())header.append(btn('新增三单匹配',matchDialog,true));const bar=tools(root),filter=select(bar,'status','处理状态',[['','全部'],['open','待处理'],['investigating','核对中'],['resolved','已处理'],['dismissed','已排除']],state.alertStatus||'');bar.append(btn('筛选',()=>{state.alertStatus=filter.value;return renderAlerts();}));
 const load=busy(root),result=await api('/reconcile/alerts&status='+encodeURIComponent(state.alertStatus||''));load.remove();
 table(root,['异常类型','来源记录','发现时间','状态','操作'],result.items.map(r=>[L(r.alert_type),r.external_id||'来源读取',r.last_seen_at,pill(r.status),btn('证据与处理',()=>alertDialog(r))]),'当前没有同步核对异常。');
}
function diffTable(parent,before,after){const keys=[...new Set([...Object.keys(before||{}),...Object.keys(after||{})])];table(parent,['字段','修改前','修改后'],keys.map(k=>{const old=before?.[k],value=after?.[k],changed=JSON.stringify(old)!==JSON.stringify(value),a=E('span',old==null?'—':typeof old==='object'?JSON.stringify(old):String(old),'fw2-wide-value'+(changed?' fw2-diff-before':'')),b=E('span',value==null?'—':typeof value==='object'?JSON.stringify(value):String(value),'fw2-wide-value'+(changed?' fw2-diff-after':''));return [({amount_cents:'含税金额（分）',tax_cents:'税额（分）',quantity:'数量',payment_kind:'款项性质',invoice_state:'发票状态',business_date:'业务日期',doc_no:'单据号',project_id:'研发项目'})[k]||k,a,b];}));}
function alertDialog(r){const m=dialog(L(r.alert_type)),evidence=r.evidence;m.content.append(E('p',evidence.reason||'请逐项核对来源与入账信息。','fw2-note'));if(evidence.before||evidence.after)diffTable(m.content,evidence.before,evidence.after);else if(evidence.totals)table(m.content,['单据类型','数量','含税金额（分）','税额（分）'],Object.entries(evidence.totals).map(([k,v])=>[L(k),...v]));
 if(writeFinance()){const status=select(m.content,'status','处理结果',[['investigating','核对中'],['resolved','已核实处理'],['dismissed','已核实排除']],r.status==='open'?'investigating':r.status),note=field(m.content,'note','核对说明与依据','textarea');submitForm(m,'保存核对结果',async()=>{await cmd('/reconcile/alerts/'+r.id+'/actions',{expectedVersion:r.version,status:status.value,note:note.value});await renderAlerts();await refreshBell();});}}
async function matchDialog(){
 const m=dialog('采购单／入库单／发票匹配'),records=(await api('/ledgers&module=warehouse&limit=500')).items.filter(r=>['approved','posted'].includes(r.status));
 const po=select(m.content,'po_id','采购单行',records.filter(x=>x.kind==='purchase_order').map(x=>[x.id,x.doc_no+' / '+x.item_code])),receipt=select(m.content,'receipt_id','入库单行',records.filter(x=>x.kind==='receipt').map(x=>[x.id,x.doc_no+' / '+x.item_code])),invoice=select(m.content,'invoice_id','发票行',records.filter(x=>x.kind==='invoice').map(x=>[x.id,x.doc_no+' / '+x.item_code]));
 const qty=field(m.content,'quantity','本次分配数量','number','1');qty.step='0.000001';const net=field(m.content,'net','不含税金额（元）','number'),tax=field(m.content,'tax','税额（元）','number','0.00');
 submitForm(m,'确认匹配',async()=>{await cmd('/reconcile/matches',{po_id:po.value,receipt_id:receipt.value,invoice_id:invoice.value,quantity:qty.value,net_amount:net.value,tax_amount:tax.value});showToast('匹配分配已保存');});
}

async function renderAccounts(){
 const {root,header}=page('module-collab-accounts','科目与辅助核算','支持 4／6／8／10 位编码。停用科目保留历史，新增明细即时用于凭证与余额表。');if(writeFinance())header.append(btn('自定义添加科目',()=>accountDialog(),true));
 const load=busy(root),result=await api('/accounts');load.remove();state.accounts=result.items;
 table(root,['编码','科目名称','级次','余额方向','辅助核算','状态','操作'],result.items.map(r=>{const name=E('span',r.name);name.style.paddingLeft=(r.level-1)*18+'px';return [r.code,name,String(r.level),r.direction,r.dimensions.map(d=>L(d.kind)+(d.required?'（必填）':'')).join('、')||'—',r.is_active?'启用':'停用',writeFinance()?actionGroup(r.level<4&&r.is_active?btn('添加下级',()=>accountDialog(null,r.code)):null,btn('编辑',()=>accountDialog(r))):'只读'];}));
 const bar=tools(root);bar.append(btn('管理辅助对象',dimensionDialog),btn('打开全级次余额表',()=>openWorkspaceTab('gl-balance')));
}
function accountDialog(record,parent=''){
 const m=dialog(record?'编辑科目':'自定义添加科目'),g=E('div','','fw2-grid');m.content.append(g);let code;
 if(record){field(g,'code','科目编码','text',record.code).disabled=true;}
 else{const parentInput=select(g,'parent','上级科目',[['','新建一级'],...state.accounts.filter(a=>a.is_active&&a.level<4).map(a=>[a.code,a.code+' '+a.name])],parent);code=field(g,'code','科目编码','text',parent?nextCode(parent):'');code.maxLength=10;parentInput.onchange=()=>{code.value=parentInput.value?nextCode(parentInput.value):'';};}
 const name=field(g,'name','科目名称','text',record?.name);const side=select(g,'direction','余额方向',[['借','借'],['贷','贷']],record?.direction||state.accounts.find(a=>a.code===parent)?.direction||'借');const category=select(g,'category','一级科目类别',['资产','负债','权益','成本','损益'].map(v=>[v,v]),record?.category||'资产');
 const active=record?check(g,'active','启用此科目',!!record.is_active):null;
 m.content.append(E('h3','辅助核算（勾选后为必填）'));const dims=E('div','','fw2-grid');m.content.append(dims);const checks={};for(const kind of ['customer','supplier','department','project','bank_account'])checks[kind]=check(dims,kind,L(kind),!!record?.dimensions.some(d=>d.kind===kind&&d.required));
 submitForm(m,'保存科目',async()=>{const dimensions=Object.entries(checks).filter(([,n])=>n.checked).map(([kind])=>({kind,required:true}));const p={name:name.value,dimensions};if(record){Object.assign(p,{expectedVersion:record.version,is_active:active.checked});await cmd('/accounts/'+record.code,p,'PATCH');}else await cmd('/accounts',{...p,code:code.value,direction:side.value,category:category.value});await apiLoadNormalizedReferenceData();await renderAccounts();document.dispatchEvent(new Event('fw-accounts-changed'));showToast('科目已保存，凭证与余额表已更新');});
}
function nextCode(parent){for(let i=1;i<=99;i++){const code=parent+String(i).padStart(2,'0');if(!state.accounts.some(a=>a.code===code))return code;}return parent+'99';}
async function dimensionDialog(){
 await loadDimensions();const m=dialog('辅助核算对象');table(m.content,['类型','编码','名称'],state.dimensions.map(r=>[L(r.kind),r.code,r.name]));if(!writeFinance())return;
 const g=E('div','','fw2-grid');m.content.append(g);const kind=select(g,'kind','类型',['customer','supplier','department','project','bank_account'].map(k=>[k,L(k)]),'project'),code=field(g,'code','对象编码'),name=field(g,'name','对象名称');submitForm(m,'新增对象',async()=>{await cmd('/dimensions',{kind:kind.value,code:code.value,name:name.value});showToast('辅助核算对象已新增');});
}
async function renderBalance(){
 const {root}=page('module-collab-balance','全级次科目余额表','按原始分录逐级汇总，保留父级历史直接发生额。下方总计不重复累计父子节点。');const bar=tools(root),month=field(bar,'period','会计期间','month',state.balancePeriod||reportPeriod()),level=select(bar,'level','展开级次',[[1,'一级'],[2,'二级'],[3,'三级'],[4,'四级']],String(state.balanceLevel||3));
 await loadDimensions();const dimension=select(bar,'dimension','辅助对象',[['','全部（含完整期初）'],...state.dimensions.map(d=>[d.id,L(d.kind)+' / '+d.name])],state.balanceDimension||'');
 const refresh=()=>{state.balancePeriod=month.value;state.balanceLevel=Number(level.value);state.balanceDimension=dimension.value;state.balanceCollapsed.clear();return renderBalance();};bar.append(btn('查询',refresh),btn('全部展开',()=>{state.balanceCollapsed.clear();return drawBalance();}));const load=busy(root);
 const data=await api('/trial-balance&period='+encodeURIComponent(month.value)+'&level='+level.value+(dimension.value?'&dimension_id='+encodeURIComponent(dimension.value):''));load.remove();if(data.note)root.append(E('p',data.note,'fw2-note'));const target=E('div');root.append(target);
 function drawBalance(){target.replaceChildren();const rows=data.items.filter(r=>![...state.balanceCollapsed].some(code=>r.code!==code&&r.code.startsWith(code))).map(r=>{const name=E('div','','fw2-tree-label');name.style.paddingLeft=(r.level-1)*16+'px';if(r.has_children){const toggle=btn(state.balanceCollapsed.has(r.code)?'＋':'−',()=>{state.balanceCollapsed.has(r.code)?state.balanceCollapsed.delete(r.code):state.balanceCollapsed.add(r.code);drawBalance();});toggle.className='fw2-tree-toggle';toggle.setAttribute('aria-label',(state.balanceCollapsed.has(r.code)?'展开':'折叠')+r.name);toggle.setAttribute('aria-expanded',String(!state.balanceCollapsed.has(r.code)));name.append(toggle);}name.append(E('span',r.name));return [r.code,name,r.opening_debit,r.opening_credit,r.debit,r.credit,r.ending_debit,r.ending_credit,btn('明细',()=>balanceDetail(r,month.value))];});
 const t=data.totals;rows.push(['合计','基础科目汇总',t.opening_debit,t.opening_credit,t.debit,t.credit,t.ending_debit,t.ending_credit,'']);const grid=table(target,['编码','科目名称','期初借方','期初贷方','本期借方','本期贷方','期末借方','期末贷方','联查'],rows);grid.tBodies[0].lastChild.classList.add('fw2-total');}
 drawBalance();bar.append(btn('导出 CSV',()=>{const rows=data.items.map(r=>[r.code,r.name,r.opening_debit,r.opening_credit,r.debit,r.credit,r.ending_debit,r.ending_credit]);exportRows('全级次科目余额表_'+month.value,['编码','名称','期初借方','期初贷方','本期借方','本期贷方','期末借方','期末贷方'],rows);}));
}
async function balanceDetail(account,period){const m=dialog(account.code+' '+account.name+' · 本期明细'),result=await api('/entries&prefix='+account.code+'&period='+period);table(m.content,['日期','凭证号','科目','摘要','借方金额','贷方金额'],result.items.map(e=>[e.voucher_date,e.voucher_no,e.account_code,e.summary,e.debit_amount,e.credit_amount]));if(result.truncated)m.content.append(E('p','此窗口显示前 500 条，请缩小期间或在明细账中查询。','fw2-note'));}

async function renderVouchers(){
 const {root}=page('module-collab-vouchers','凭证审批与过账','制单后提交至流程中心，由另一位财务总监终审。已过账凭证通过红冲申请更正。');const bar=tools(root);bar.append(btn('刷新',renderVouchers),btn('打开流程中心',()=>openWorkspaceTab('workflow-center')),writeFinance()?btn('录入凭证',()=>openWorkspaceTab('gl-voucher'),true):E('span'));
 const load=busy(root),result=await apiRequest('/api/vouchers'+qs()+'&limit=200');load.remove();
 table(root,['日期','凭证号／摘要','制单人','终审人','状态','操作'],result.items.map(v=>[v.voucher_date,`${v.voucher_no} / ${v.summary}`,v.created_by||'历史身份待核验',v.reviewed_by||'—',pill(v.status==='PENDING'&&v.reviewed_at?'approved':v.status.toLowerCase()),actionGroup(btn('查看',()=>{const m=dialog(v.voucher_no);showSubject(m.content,v);}),writeFinance()&&['DRAFT','PENDING'].includes(v.status)&&!v.reviewed_at&&v.source_type!=='VAT_CLOSE'?btn('提交审批',async()=>{await cmd('/vouchers/'+v.id+'/submit',{expectedVersion:v.version});showToast('已提交到流程中心');await refreshBell();}):null,writeFinance()&&v.reviewed_at&&['DRAFT','PENDING'].includes(v.status)?btn('过账',async()=>{await apiRequest('/api/vouchers/'+v.id+'/post'+qs(),{method:'POST'});await renderVouchers();await refreshLegacyVouchers();}):null,writeFinance()&&v.status==='POSTED'?btn('申请红冲',()=>reversalDialog(v)):null)]));
}
async function refreshLegacyVouchers(){if(storageMode==='API'&&finance()){apiStoreCache.set(STORE_VOUCHER,await apiListNormalizedVouchers());window.renderLiveBalance?.();}}
function reversalDialog(v){const m=dialog('申请红冲 '+v.voucher_no),day=field(m.content,'date','红冲凭证日期','date',new Date().toLocaleDateString('sv-SE')),reason=field(m.content,'reason','红冲原因','textarea');submitForm(m,'提交独立终审',async()=>{await cmd('/vouchers/'+v.id+'/reversals',{expectedVersion:v.version,voucher_date:day.value,reason:reason.value});await refreshBell();showToast('红冲申请已提交');});}

async function renderVAT(){
 const {root}=page('module-collab-vat','进项与销项期末结转','按已过账税务明细测算，生成凭证草稿并独立终审。留抵或零差额只确认结果。');const bar=tools(root),month=field(bar,'period','会计期间','month',state.vatPeriod||reportPeriod()),opening=field(bar,'opening','首次启用的期初留抵（元）','number',state.vatOpening||'0.00');
 const body=E('div');root.append(body);let preview=null;
 const calculate=async()=>{state.vatPeriod=month.value;state.vatOpening=opening.value;body.replaceChildren();const load=busy(body);try{preview=await api('/tax/vat/'+month.value+'/preview','POST',{opening_credit:opening.value});load.remove();const list=E('dl','','fw2-kpi-list');for(const [label,value] of [['本期净销项',preview.output],['可抵扣进项净额',preview.input],['上期留抵',preview.previous_credit]]){const n=E('div');n.append(E('dt',label+'（元）'),E('dd',value));list.append(n);}body.append(list);table(body,['差额','应转未交增值税','本期期末留抵'],[[preview.difference,preview.due,preview.credit]]);if(preview.requires_manual_review)body.append(E('p',preview.review_reason,'fw2-error'));if(preview.closing){const r=preview.closing;body.append(E('p',`本期结转：${L(r.status)}${r.is_stale?' · 基础已变化，需重新处理':''}${r.voucher_id?' · 关联凭证 #'+r.voucher_id:''}`,'fw2-note'));const actions=tools(body);if(writeFinance()&&r.status==='draft')actions.append(btn('提交结转终审',async()=>{await cmd('/tax/vat/'+r.id+'/submit');await calculate();await refreshBell();},true));if(writeFinance()&&r.status==='approved'&&r.voucher_id)actions.append(btn('过账结转凭证',async()=>{await apiRequest('/api/vouchers/'+r.voucher_id+'/post'+qs(),{method:'POST'});await calculate();await refreshLegacyVouchers();},true));if(writeFinance()&&['draft','submitted','approved'].includes(r.status))actions.append(btn('取消本次结转',async()=>{await cmd('/tax/vat/'+r.id+'/cancel',{reason:'财务取消后重新测算'});await calculate();}));actions.append(btn('查看凭证与流程',()=>openWorkspaceTab(r.voucher_id?'collab-vouchers':'workflow-center')));}else if(writeFinance()&&!preview.requires_manual_review){body.append(btn(Number(preview.due)>0?'一键生成结转凭证':'保存留抵／零差额确认单',async()=>{await cmd('/tax/vat/'+month.value+'/generate',{basisHash:preview.basis_hash,opening_credit:opening.value});await calculate();await refreshLegacyVouchers();},true));}
 }catch(e){load.textContent=e.message;}};
 bar.append(btn('测算本期税额',calculate,true));root.append(E('p','待认证进项仅在正式转入后计入。已缴、预缴、减免或退税等特殊事项需先复核；本功能不将进销项专栏强制清零。','fw2-note'));await calculate();
}

async function renderAudit(offset=0){const {root}=page('module-collab-audit','操作审计日志','查看同步、台账、凭证、权限及流程的操作人、时间与修改前后快照。');const load=busy(root),result=await api('/audit-logs&limit=50&offset='+offset);load.remove();table(root,['操作时间','所属模块','操作','修改人','记录','对比'],result.items.map(r=>[r.acted_at,r.module,r.operation,r.actor,r.record_id,btn('新旧值对比',()=>{const m=dialog('审计对比 · '+r.actor+' · '+r.acted_at);diffTable(m.content,r.before_data,r.after_data);})]));const bar=tools(root);bar.append(btn('上一页',()=>renderAudit(Math.max(0,offset-50))),btn('下一页',()=>renderAudit(offset+50)));bar.lastChild.disabled=result.items.length<50;}
async function auditDialog(id,module){const m=dialog('记录变更历史'),result=await api('/audit-logs&record_id='+encodeURIComponent(id)+'&module='+module);for(const r of result.items){m.content.append(E('h3',r.acted_at+' · '+r.actor+' · '+r.operation));diffTable(m.content,r.before_data,r.after_data);}if(!result.items.length)m.content.append(E('p','暂无变更记录。'));}

// /members 返回的是「角色授权行」，展示前按 user_name 合并为成员列表。
function memberRows(items){
 const groups=new Map();
 for(const r of items||[]){
  if(!r.active)continue;
  const name=String(r.user_name||r.name||'').trim();if(!name)continue;
  let scopes=r.scopes_json??r.scopes??[];
  if(typeof scopes==='string'){try{scopes=JSON.parse(scopes);}catch{scopes=[];}}
  if(!Array.isArray(scopes))scopes=[];
  const member=groups.get(name)||{name,roles:[],scopes:[],isCreator:false,creatorKnown:false};
  if(r.role&&!member.roles.includes(r.role))member.roles.push(r.role);
  member.scopes=[...new Set([...member.scopes,...scopes])];
  // 新后端可对每条授权行提供明确的创建者标识；旧后端采取保守保护。
  member.creatorKnown ||= ['is_creator','is_default_creator','is_owner','is_primary_admin'].some(key=>Object.hasOwn(r,key));
  member.isCreator ||= ['is_creator','is_default_creator','is_owner','is_primary_admin'].some(key=>r[key]===true||r[key]===1);
  groups.set(name,member);
 }
 return [...groups.values()];
}
function memberProtectionReason(member){
 const current=state.access?.name||fwActor?.name;
 if(member.name===current||member.name===fwActor?.name)return '禁止删除当前登录账号';
 const creator=state.access?.creator_name||state.access?.owner_name||state.access?.created_by;
 if(member.isCreator||creator&&member.name===creator)return '禁止删除默认创建者／主理人';
 if(member.roles.includes('director')){
  if(state.members.filter(x=>x.roles.includes('director')).length<=1)return '必须保留至少一位财务总监';
  if(!member.creatorKnown&&!creator)return '服务端尚未标记创建者，暂时保护所有财务总监账号';
 }
 return '';
}
async function renderMembersContent(root){
 if(!configure()){
  root.append(E('p','当前账号没有成员管理权限。仅财务总监可查看、添加、修改或删除成员。','fw2-note'));
  return;
 }
 const load=busy(root),result=await api('/members');load.remove();
 state.members=memberRows(result.items);
 table(root,['账号','有效角色','填报范围','操作'],state.members.map(r=>{
  const reason=memberProtectionReason(r);
  const remove=btn('删除',()=>removeMemberDialog(r));remove.classList.add('fw2-danger-button');
  if(reason){remove.disabled=true;remove.title=reason;remove.setAttribute('aria-label',`不能删除 ${r.name}：${reason}`);}
  return [r.name,r.roles.map(L).join('、'),r.scopes.map(L).join('、')||'—',
   actionGroup(btn('调整权限',()=>memberDialog(r)),btn('修改密码',()=>changeMemberPasswordDialog(r)),remove)];
 }), '当前账套尚无成员。');
 root.append(E('p','保护当前登录账号、默认创建者和最后一位财务总监。权限变更及删除必须由服务端再次校验。','fw2-note'));
}
async function renderSettings(){
 const {root,header}=page('module-collab-settings','协作成员与权限',
  '角色按当前账套分配，可兼任。终审仍须由另一人完成；撤销角色立即影响数据访问与未办任务。');
 if(configure())header.append(btn('新增／调整成员',()=>memberDialog(),true));
 await renderMembersContent(root);
 root.append(E('p','首次启用需至少一位财务总监和一位会计；财务申请不得由发起人自行终审。','fw2-note'));
 // 保留原有纳税人类型设置，避免重构误删账套配置功能。
 const taxPanel=E('section','','fw2-tax-setting');
 taxPanel.append(E('h3','账套纳税人类型'));
 root.append(taxPanel);
 const settings=await api('/book-settings'),taxTools=tools(taxPanel),kind=select(taxTools,'taxpayer_type','当前账套纳税人类型',
  [['general','一般纳税人'],['small_scale','小规模纳税人']],settings.taxpayer_type);
 taxTools.append(btn('保存纳税人类型',async()=>{
  await cmd('/book-settings',{taxpayer_type:kind.value},'PUT');showToast('纳税人类型已保存');
 }));
}
async function renderCollabHub(){
 const {root}=page('module-collab-hub','协作与同步',
  '集中管理账套协作成员、角色权限以及飞书／Google Sheets 外部表格的同步连接。');
 const grid=E('div','','fw2-collab-grid');root.append(grid);
 const members=E('section','','fw2-collab-panel'),sync=E('section','','fw2-collab-panel');
 grid.append(members,sync);
 const memberHead=E('div','','fw2-collab-panel-header'),syncHead=E('div','','fw2-collab-panel-header');
 memberHead.append(E('h2','协作成员与权限'));
 syncHead.append(E('h2','外部表格与同步'));
 if(configure()){
  memberHead.append(btn('新增成员',()=>memberDialog(),true));
  syncHead.append(btn('新增表格连接',()=>connectionDialog(),true));
 }
 members.append(memberHead,E('p','按当前账套管理成员权限与登录凭证。','fw2-note'));
 sync.append(syncHead,E('p','连接管理、定时／手动同步及连接解绑。','fw2-note'));
 await Promise.all([renderMembersContent(members),renderSyncContent(sync)]);
}
async function refreshCollabSurface(section){
 if(activeTabKey==='collab-hub')return renderCollabHub();
 return section==='sync'?renderSync():renderSettings();
}

async function renderPeriods(){
 const {root}=page('module-collab-periods','账期锁定与解锁','会计发起，总监独立终审。锁定前检查凭证、同步异常和税务结转；解锁保留原结账快照。');
 const bar=tools(root),period=field(bar,'period','会计期间','month',state.controlPeriod||reportPeriod()),reason=field(bar,'reason','申请原因');const results=E('div');root.append(results);
 const inspect=async()=>{state.controlPeriod=period.value;const r=await api('/periods/'+period.value+'/check');results.replaceChildren();const names={no_unposted_vouchers:'本期凭证全部过账',trial_balance_balanced:'本期借贷平衡',bank_items_processed:'银行差异已处理',cashier_records_processed:'资金记录已处理',no_open_financial_approvals:'财务审批已完成',no_unresolved_sync_alerts:'同步异常已核对',vat_close_confirmed:'税务结转已确认',no_unprocessed_business_ledgers:'业务台账已处理'};table(results,['结账检查','结果'],Object.entries(r.checks).map(([k,v])=>[names[k]||k,v?'通过':'待处理']));};
 bar.append(btn('检查本期',inspect));if(writeFinance())for(const [action,label] of [['close-requests','申请锁定'],['unlock-requests','申请解锁']])bar.append(btn(label,async()=>{await cmd('/periods/'+period.value+'/'+action,{reason:reason.value});await refreshBell();showToast('已提交流程中心，由另一位财务总监终审');}));
 const records=await api('/periods');table(root,['期间','状态','变更原因','锁定时间'],records.items.map(r=>[r.period,r.is_locked?'已锁定':'开放',r.lock_reason||'—',r.locked_at||'—']));await inspect();
}
function memberDialog(r){
 const m=dialog(r?'调整成员权限':'新增账套成员'),name=field(m.content,'name','账号','text',r?.name);
 if(r)name.disabled=true;
 const password=!r?field(m.content,'password','新账号密码（至少 10 位）','password'):null;
 if(password){password.required=true;password.minLength=10;password.maxLength=256;password.autocomplete='new-password';}
 const rolebox=E('div','','fw2-grid');m.content.append(E('h3','角色'),rolebox);
 const roleInputs={};for(const role of ['director','accountant','cashier','reporter'])roleInputs[role]=check(rolebox,role,L(role),r?r.roles.includes(role):role==='accountant');
 const scopes=E('div','','fw2-grid');m.content.append(E('h3','填报员模块范围'),scopes);
 const scopeInputs={};for(const scope of ['warehouse','expense','asset'])scopeInputs[scope]=check(scopes,scope,L(scope),r?.scopes.includes(scope));
 submitForm(m,'保存成员权限',async()=>{
  const p={name:name.value,roles:Object.entries(roleInputs).filter(([,n])=>n.checked).map(([k])=>k),
   scopes:Object.entries(scopeInputs).filter(([,n])=>n.checked).map(([k])=>k)};
  if(password){if(password.value.length<10)throw Error('密码至少需要 10 位');p.password=password.value;}
  await cmd('/members',p);if(password)password.value='';
  await refreshCollabSurface('members');showToast('成员权限已保存');
 });
}
function changeMemberPasswordDialog(member){
 if(!configure())throw Error('当前账号没有修改成员密码的权限');
 const m=dialog('修改密码');
 m.content.append(E('p','正在修改成员：'+member.name,'fw2-note'));
 const password=field(m.content,'new_password','新密码','password');
 const confirm=field(m.content,'confirm_password','确认新密码','password');
 for(const input of [password,confirm]){
  input.required=true;input.minLength=10;input.maxLength=256;input.autocomplete='new-password';
 }
 m.content.append(E('p','密码至少 10 位。只发送到服务端安全更新，不写入浏览器本地存储。','fw2-note'));
 submitForm(m,'保存新密码',async()=>{
  const value=password.value;
  if(!value.trim())throw Error('请输入新密码');
  if(value.length<10||value.length>256)throw Error('新密码长度须为 10～256 位');
  if(value!==confirm.value)throw Error('两次输入的密码不一致');
  // 用户认证凭证存于服务端；后端必须哈希密码并使旧会话失效。
  await api('/members/'+encodeURIComponent(member.name)+'/password','PUT',
   {requestKey:requestKey(),password:value});
  password.value='';confirm.value='';showToast('成员「'+member.name+'」的密码已更新');
 });
 password.focus();
}
function removeMemberDialog(member){
 if(!configure())throw Error('当前账号没有删除成员的权限');
 const reason=memberProtectionReason(member);
 if(reason)throw Error(reason);
 const m=dialog('确认删除协作成员');
 const message=E('p','','fw2-destructive-notice');
 message.append('即将从当前账套删除成员「',E('strong',member.name),'」的全部角色与访问权限。该操作不可撤销。');
 m.content.append(message,E('p','成员删除不应清除既有业务单据和审计历史；服务端须同时撤销此成员的未办任务及访问授权。','fw2-note'));
 submitForm(m,'确认删除',async()=>{
  const latest=state.members.find(item=>item.name===member.name);
  if(!latest)throw Error('成员列表已更新，请刷新后重试');
  const currentReason=memberProtectionReason(latest);
  if(currentReason)throw Error(currentReason);
  // 数据库是权威来源。只有 API 确认删除成功后才更新页面状态。
  await api('/members/'+encodeURIComponent(member.name),'DELETE',{requestKey:requestKey()});
  state.members=state.members.filter(item=>item.name!==member.name);
  await refreshCollabSurface('members');showToast('成员「'+member.name+'」已从当前账套移除');
 });
 m.foot.querySelector('[type=submit]').classList.add('fw2-danger-submit');
}

fwControlDialog=function(required=false){
 const m=dialog('账户与财务权限'),name=field(m.content,'name','账号'),password=field(m.content,'password','密码（至少 10 位）','password');name.autocomplete='username';password.autocomplete='current-password';
 if(required){m.foot.replaceChildren();m.d.addEventListener('cancel',e=>e.preventDefault());}
 const login=async()=>{const result=await apiRequest('/api/control/login',{method:'POST',body:JSON.stringify({name:name.value,password:password.value})});sessionStorage.setItem('fw-control-token',result.token);location.reload();};
 submitForm(m,'登录',login);
 void (async()=>{try{const status=await apiRequest('/api/control/status');if(status.actor){m.content.prepend(E('p','当前账号：'+status.actor.name,'fw2-note'));name.parentElement.hidden=password.parentElement.hidden=true;m.foot.querySelector('[type=submit]').hidden=true;m.foot.prepend(btn('退出登录',async()=>{await apiRequest('/api/control/logout',{method:'POST'});sessionStorage.removeItem('fw-control-token');location.reload();}));if(configure())m.foot.prepend(btn('管理协作成员',()=>{m.d.close();openWorkspaceTab('collab-settings');}));}else if(!status.enabled){m.content.prepend(E('p','先创建首位财务总监，再为账套添加会计、出纳和填报员。','fw2-note'));m.foot.querySelector('[type=submit]').textContent='创建总监并启用权限';m.form.onsubmit=async e=>{e.preventDefault();const b=m.form.querySelector('[type=submit]');b.disabled=true;try{await apiRequest('/api/control/setup',{method:'POST',body:JSON.stringify({name:name.value,password:password.value})});await login();}catch(err){m.error.textContent=err.message;b.disabled=false;}};}}catch(e){m.error.textContent=e.message;}})();return m.d;
};
window.fwCollabInit=async function(){
 state.access=window.FW_V2_ACCESS||null;
 if(state.access){state.prefs=await api('/preferences');shortcutVersion=state.prefs.version;}
 else{try{state.prefs=JSON.parse(localStorage.getItem('fw-demo-shortcuts:'+SUITE_ACTIVE.id)||'null');}catch{state.prefs=null;}}
 applyNavigation();await renderHome();
 document.getElementById('fw-guide').onclick=()=>openWorkspaceTab('finance-guide');
 let pending=false;
 const refresh=()=>{if(!pending){pending=true;requestAnimationFrame(()=>{pending=false;refreshAccountPickers();});}};
 new MutationObserver(records=>{if(records.some(record=>Array.from(record.addedNodes).some(node=>node.nodeType===1&&(node.matches('select,option')||node.querySelector('select')))))refresh();}).observe(document.querySelector('.workspace-content'),{childList:true,subtree:true});
 refresh();
 if(window.FW_V2_RESTRICTED){
  const picker=document.getElementById('suite-book-picker');picker.replaceChildren(...suiteBooks().map(b=>new Option(b.name,b.id)));picker.value=SUITE_ACTIVE.id;
  picker.onchange=()=>{localStorage.setItem('fw-suite:active',JSON.stringify(picker.value));location.reload();};
  for(const id of ['suite-book-manage','workbench-save','fw-more'])document.getElementById(id).hidden=true;
  const search=document.getElementById('fw-command-input'),results=document.getElementById('fw-command-results');
  search.oninput=()=>{results.replaceChildren();const found=shortcutChoices().filter(c=>!c.key.startsWith('group-')&&c.label.includes(search.value.trim()));results.hidden=!search.value.trim();for(const item of found){const b=btn(item.label,()=>{results.hidden=true;search.value='';openWorkspaceTab(item.key);});b.setAttribute('role','option');results.append(b);}};
  document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();search.focus();}});
 }
 const actions=document.querySelector('.app-header .workbench-actions');
 if(!document.getElementById('fw2-bell')){const bell=btn('',toggleInbox);bell.id='fw2-bell';bell.classList.add('fw2-bell');bell.setAttribute('aria-label','待办提醒');bell.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9ZM10 21h4"/></svg><span>待办</span><span id="fw2-badge" class="fw2-badge" hidden></span>';actions.prepend(bell,btn('账户',()=>fwControlDialog()));}
 if(!document.getElementById('fw2-center-link')){const link=btn('流程中心',()=>{ensure();openWorkspaceTab('workflow-center');});link.id='fw2-center-link';link.classList.add('sidebar-home');document.getElementById('nav-home-btn').after(link);}
 if(state.access){await refreshBell();if(poll)clearInterval(poll);poll=setInterval(()=>{if(!document.hidden)void refreshBell().catch(()=>{});},30000);}
 if(window.FW_V2_RESTRICTED&&roles().has('cashier')){apiStoreCache.set(STORE_CASHIER,await apiListNormalizedCashier());initCashierModule();}
 document.querySelectorAll('#fw-new-user,#fw-new-role').forEach(n=>n.hidden=true);
};
window.fwV2ReversalDialog=v=>reversalDialog({id:v.dbId,version:v.version,voucher_no:v.voucherNo});
window.fwV2EntryDimensions=async function(tr,existing={}){
 if(!window.FW_V2_ACCESS?.capabilities.financial_read)return;
 tr.dataset.dimensions=JSON.stringify(existing);
 const code=tr.querySelector('.entry-account').value;
 try{
  const [accountData,dimensionData]=await Promise.all([api('/accounts'),api('/dimensions')]);
  if(!tr.isConnected||tr.querySelector('.entry-account').value!==code)return;
  tr.querySelector('.fw2-entry-dimensions')?.remove();
  const config=accountData.items.find(a=>a.code===code)?.dimensions||[];
  if(!config.length)return;
  const details=E('details','','fw2-entry-dimensions');details.append(E('summary','辅助维度（'+config.length+'）'));
  const values={...existing};
  for(const d of config){const input=select(details,d.kind,L(d.kind)+(d.required?' *':''),[['','请选择'],...dimensionData.items.filter(x=>x.kind===d.kind).map(x=>[x.id,x.name])],existing[d.kind]||'');input.onchange=()=>{if(input.value)values[d.kind]=input.value;else delete values[d.kind];tr.dataset.dimensions=JSON.stringify(values);};}
  tr.querySelector('.entry-aux').parentElement.append(details);
 }catch(e){showToast('辅助核算读取失败：'+e.message);}
};
document.addEventListener('fw-accounts-changed',()=>{for(const selectInput of document.querySelectorAll('.entry-account')){const selected=selectInput.value;selectInput.replaceChildren(...VOUCHER_ACCOUNTS.filter(a=>a.isActive!==false).map(a=>new Option(a.code+' '+a.name,a.code)));selectInput.value=selected;void window.fwV2EntryDimensions(selectInput.closest('tr'),JSON.parse(selectInput.closest('tr').dataset.dimensions||'{}'));}});
document.addEventListener('click',e=>{if(inbox&&!inbox.contains(e.target)&&!document.getElementById('fw2-bell')?.contains(e.target)){inbox.remove();inbox=null;}});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&inbox){inbox.remove();inbox=null;}});
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state.access)void refreshBell().catch(()=>{});});
})();




