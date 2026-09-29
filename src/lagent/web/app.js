(function(){
"use strict";
const $ = (s,r)=> (r||document).querySelector(s);
const $$ = (s,r)=> Array.from((r||document).querySelectorAll(s));
const esc = s => String(s==null?"":s).replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

/* ---------- 主题 ---------- */
/* 存储键升到 -v2：老用户 localStorage 里存着 'dark'，
   沿用旧键的话刷新一次就被记忆弹回深色，本次纸感改版等于白做。 */
const THEME_KEY="lagent-theme-v2";
function applyTheme(t){
  document.documentElement.setAttribute("data-theme",t);
  $("#btn-theme").innerHTML = t==="dark"
    ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"/></svg>'
    : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z"/></svg>';
  try{localStorage.setItem(THEME_KEY,t);}catch(e){}
}
/* 默认「纸感浅色」，不再跟随系统：深色是一个可选口味，由按钮切换，不是默认值 */
(function initTheme(){
  let saved=null; try{saved=localStorage.getItem(THEME_KEY);}catch(e){}
  applyTheme(saved || "light");
})();
$("#btn-theme").addEventListener("click",()=>{
  applyTheme(document.documentElement.getAttribute("data-theme")==="dark"?"light":"dark");
});

/* ---------- 令牌存储 ----------
   token 放 sessionStorage 而不是 localStorage：关掉标签页即失效，
   把「令牌被 XSS 偷走」的利用窗口从永久压到本次会话。 */
const TOKEN_KEY="lagent-token";
const tokenApi={
  get(){try{return sessionStorage.getItem(TOKEN_KEY);}catch(e){return null;}},
  set(v){try{v?sessionStorage.setItem(TOKEN_KEY,v):sessionStorage.removeItem(TOKEN_KEY);}catch(e){}}
};

/* ---------- 基础工具 ---------- */
function toast(msg,kind){
  const el=document.createElement("div");
  el.className="toast "+(kind||"");
  el.textContent=msg;
  $("#toasts").appendChild(el);
  setTimeout(()=>{el.style.opacity="0";setTimeout(()=>el.remove(),240);},3200);
}
async function api(path,opts){
  const o=Object.assign({},opts||{});
  o.headers=Object.assign({"Content-Type":"application/json"},o.headers||{});
  const t=tokenApi.get();
  if(t) o.headers.Authorization="Bearer "+t;
  const r=await fetch(path,o);
  let body=null;
  try{body=await r.json();}catch(e){}
  if(r.status===401){
    // 令牌缺失/过期不是"一次普通请求失败"，而是会话结束 —— 直接回登录闸门
    sessionExpired();
    throw new Error("登录状态已失效，请重新登录");
  }
  if(!r.ok){
    const detail=(body&&(body.detail||body.message))||("HTTP "+r.status);
    const err=new Error(detail); err.status=r.status; throw err;
  }
  return body;
}
const SKEL = {card:'<div class="skel sk-card"></div>',row:'<div class="skel sk-row"></div>'};
const skel=(n,t)=>SKEL[t||"row"].repeat(n);

/* ---------- 状态 ---------- */
const state={ users:[], equipment:new Map(), session:"web-"+Math.random().toString(36).slice(2,8), busy:false, me:null };
const isAdmin = ()=> !!state.me && (state.me.role==="admin"||state.me.role==="sysadmin");

/* ---------- 登录闸门 ---------- */
function gateError(msg){ const el=$("#lg-err"); el.textContent=msg||""; el.classList.toggle("on",!!msg); }
function showGate(msg){
  $("#gate").hidden=false;
  $("#badge-user").hidden=true;
  $("#btn-logout").hidden=true;
  $("#tab-users").hidden=true;
  $("#pane-users").hidden=true;
  // 退出后不能残留上一个身份的数据
  $("#labs-box").innerHTML=skel(2,"card");
  $("#res-box").innerHTML=skel(3,"row");
  $("#kb-box").innerHTML="";
  $("#users-box").innerHTML=skel(3,"row");
  $("#lg-pass").value="";
  gateError(msg||"");
}
function clearSession(){ tokenApi.set(null); state.me=null; state.users=[]; $("#msgs").innerHTML=""; }
function sessionExpired(){ clearSession(); showGate("登录状态已失效，请重新登录"); }

async function doLogin(){
  const username=$("#lg-user").value.trim(), password=$("#lg-pass").value;
  if(!username||!password){ gateError("请填写用户名与密码"); return; }
  const btn=$("#lg-go"); btn.disabled=true; btn.textContent="登录中…"; gateError("");
  try{
    // 登录本身不能用 api()（它会在 401 时把闸门重置掉，错误提示就看不见了）
    const r=await fetch("/api/auth/login",{
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({username,password})});
    const body=await r.json().catch(()=>null);
    if(!r.ok){ gateError((body&&body.detail)||("登录失败（HTTP "+r.status+"）")); return; }
    tokenApi.set(body.access_token);
    state.me=body.user;
    $("#gate").hidden=true;
    await enterApp();
    toast("已登录："+body.user.username,"ok");
  }catch(e){ gateError("无法连接后端："+e.message); }
  finally{ btn.disabled=false; btn.textContent="登录"; }
}

function renderIdentity(){
  const me=state.me||{};
  const certs=me.certs||[];
  $("#badge-user").hidden=false;
  $("#btn-logout").hidden=false;
  $("#who-name").textContent=me.username||"–";
  const role=$("#who-role");
  role.textContent=me.role||"";
  role.className="who-role"+(isAdmin()?" admin":"");
  $("#identity-note").innerHTML=
    '以 <b style="color:var(--text)">'+esc(me.username||"")+'</b> 的身份对话（身份取自令牌，不由前端指定）· 准入资质：'
    +(certs.length?esc(certs.join(" / ")):'<span style="color:var(--warn)">无</span>');
  $("#tab-users").hidden=!isAdmin();
}

async function enterApp(){
  renderIdentity();
  await Promise.all([loadLabs(), loadUserDirectory()]);
  fillBookingOptions();   // 设备下拉依赖 loadLabs 填好的 state.equipment
  // 管理员专属标签：非管理员一律保持 hidden。
  // ⚠️ 这只是"不显示无权限的控件"，**真正的边界在服务端** ——
  // 手动构造请求照样只会拿到 403（见 tests/test_booking_api.py 的越权清单）。
  if(isAdmin()){
    ["tab-users","tab-appr","tab-viol","tab-notif","tab-audit"]
      .forEach(id=>{ $("#"+id).hidden=false; });
  }
  await loadReservations();
  stamp("#res-box");
  await loadHealth();
  // 会变的面板开始自动刷新（2026-09-29）—— 后台 sweep 改了数据，
  // 页面不该等着人来点一下才知道。
  startAutoRefresh();
}

/* ---------- 顶部与统计 ---------- */
async function loadHealth(){
  /* 健康详情需要登录。这里刻意**不走 api()**：
     api() 在 401 时会调 sessionExpired() 并把错误抛进来，
     于是 boot() 在**未登录**状态下第一次加载时，会把"还没登录"
     显示成红点"连接失败" —— 把凭证问题误报成网络问题，
     正是本项目 everywhere else 都在避免的那类两类失败混报。
     可见状态分四档：
       未登录（灰）→ 登录过期（黄，回闸门）→ 连接失败（红）→ 正常（绿/黄）。 */
  try{
    const t=tokenApi.get();
    if(!t){
      $("#dot-health").className="dot warn";
      $("#badge-mode").innerHTML='<span class="dot warn" id="dot-health"></span>未登录';
      return;
    }
    const r=await fetch("/api/health/details",{headers:{"Authorization":"Bearer "+t}});
    if(r.status===401){
      $("#dot-health").className="dot warn";
      $("#badge-mode").innerHTML='<span class="dot warn" id="dot-health"></span>登录已过期';
      sessionExpired();
      return;
    }
    if(!r.ok) throw new Error("HTTP "+r.status);
    const h=await r.json();
    $("#dot-health").className="dot"+(h.agent_available?"":" warn");
    $("#badge-mode").innerHTML='<span class="dot'+(h.agent_available?"":" warn")+'" id="dot-health"></span>'+esc(h.app_mode)+" · "+esc(h.database);
    $("#badge-backend").textContent=h.retrieval_backend+(h.retrieval_degraded_reason?"（降级）":"");
    $("#badge-now").textContent=(h.now||"").slice(11,16);
    $("#badge-agent").innerHTML = h.agent_available
      ? '<span class="dot"></span>Agent 在线' : '<span class="dot warn"></span>Agent 降级（引导式表单）';
    $("#st-labs").textContent=h.counts.labs;
    $("#st-equip").textContent=h.counts.equipment;
    $("#st-active").textContent=h.counts.active;
    $("#st-res").textContent=h.counts.reservations;
    if(h.retrieval_degraded_reason) toast("检索已降级："+h.retrieval_degraded_reason,"err");
  }catch(e){
    $("#dot-health").className="dot bad";
    $("#badge-mode").innerHTML='<span class="dot bad"></span>连接失败';
    toast("无法连接后端："+e.message,"err");
  }
}

/* ---------- 用户目录（管理员） ---------- */
async function loadUserDirectory(){
  const box=$("#users-box"), filter=$("#flt-user");
  if(!isAdmin()){
    // 非管理员：/api/users 是管理端点（403），前端直接不请求也不显示筛选器。
    // 注意这只是"省一次无用请求 + 不显示无权限的控件"，
    // **真正的边界在服务端** —— 就算手动构造请求也只会拿到 403。
    filter.hidden=true;
    box.innerHTML='<div class="empty">当前身份无权查看用户目录（该端点需要管理员）</div>';
    return;
  }
  filter.hidden=false;
  box.innerHTML=skel(3,"row");
  try{
    state.users=await api("/api/users");
    filter.innerHTML='<option value="">全部用户</option>'
      + state.users.map(u=>`<option value="${u.id}">${esc(u.username)}</option>`).join("");
    box.innerHTML='<table><thead><tr><th>ID</th><th>用户</th><th>角色</th><th>准入资质</th></tr></thead><tbody>'
      + state.users.map(u=>{
          const roleTag = u.role==="user" ? '<span class="tag">user</span>'
            : '<span class="tag warn">'+esc(u.role)+'</span>';
          const certs=(u.certs||[]).map(c=>'<span class="tag">'+esc(c)+'</span>').join(" ")
            || '<span style="color:var(--text-faint)">无</span>';
          return `<tr><td class="mono">#${u.id}</td><td>${esc(u.username)}</td>
            <td>${roleTag}</td><td>${certs}</td></tr>`;
        }).join("")
      + '</tbody></table>';
  }catch(e){box.innerHTML='<div class="empty">加载失败：'+esc(e.message)+"</div>";}
}

/* ---------- 管理员面板：审批 / 违约 / 通知 / 审计 ----------
   这四个面板是试运行之后补的，理由写在 docs/TRIAL-RUN.md 的 H1：
   这四个能力的接口与测试早就齐了，但控制台**一个入口都没有** ——
   管理员只能 curl 或者读库，等于这些功能没交付到他手上。
   与当初"预约只能靠跟模型说话"是同一类问题：**后端有接口 ≠ 用户点得到。** */
const fmtStamp = iso => iso ? String(iso).replace("T"," ").slice(5,16) : "";

function fillUserSelect(sel){
  if(!state.users.length){ sel.innerHTML='<option value="">（用户未加载）</option>'; return; }
  const prev=sel.value;
  sel.innerHTML=state.users.map(u=>`<option value="${u.id}">${esc(u.username)}</option>`).join("");
  if(prev) sel.value=prev;
}

/* ---- 数据新鲜度（2026-09-29 加）----
   改版前所有面板都是"点一次加载一次"，后台 sweep 跑完（过期预约清扫、违约判定）
   页面不会自己更新 —— 使用者盯着一个过期的数字做审批。
   现在：面板标题上带"更新于 HH:MM:SS"，会变的面板每 30 秒自动拉一次。

   为什么是 30 秒而不是 3 秒：这些数据的变化来自后台任务（间隔 300 秒），
   拉太勤只是徒增请求。真正需要"秒级"的是任务进度那种，那是另一个场景。 */
const REFRESH_MS = 30000;

function stamp(boxSel){
  const box = $(boxSel); if(!box) return;
  const card = box.closest ? box.closest(".card") : null; if(!card) return;
  const h = card.querySelector("h2"); if(!h) return;
  let s = h.querySelector(".stamp");
  if(!s){ s = document.createElement("span"); s.className = "stamp"; h.appendChild(s); }
  s.textContent = " · 更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

/** 只刷"会变"的面板。登录后由 enterApp 启动。 */
function startAutoRefresh(){
  if(window.__autoRefresh){ clearInterval(window.__autoRefresh); }
  window.__autoRefresh = setInterval(async () => {
    if(document.hidden) return;          // 页面在后台就别拉了
    await Promise.allSettled([loadApprovals(), loadReservations()]);
    // 时间戳在加载**之后**打，否则会在数据还没回来时就宣称"已更新"
    stamp("#appr-box"); stamp("#res-box");
  }, REFRESH_MS);
}

async function loadApprovals(){
  const box=$("#appr-box");
  box.innerHTML=skel(2,"row");
  try{
    // 待办不带"全部预约"——列表里出现的每一条都是要处理的，这是那个接口
    // 刻意不复用 list_reservations 的原因（见 pending_reservations 的注释）。
    const rows=await api("/api/reservations/pending");
    if(!rows.length){ box.innerHTML='<div class="empty">没有待确认的申请</div>'; stamp("#appr-box"); return; }
    // 批量：勾几条一起批。待办一多，逐条点是最典型的机械重复。
    box.innerHTML='<div class="row batchbar">'
        +'<button class="btn sm" id="b-all">全选</button>'
        +'<button class="btn sm" id="b-none">清空</button>'
        +'<button class="btn sm" id="b-approve">批量通过</button>'
        +'<button class="btn sm ghost" id="b-reject">批量驳回</button>'
        +'<span class="hint" id="b-hint">已选 0 条</span>'
      +'</div>'
      +'<table><thead><tr><th style="width:34px"></th><th>ID</th><th>申请人</th><th>设备</th><th>时段</th><th></th></tr></thead><tbody>'
      + rows.map(r=>`<tr>
          <td><input type="checkbox" class="b-pick" value="${r.id}" aria-label="选择申请 #${r.id}"></td>
          <td class="mono">#${r.id}</td>
          <td>${esc(r.user_name||"（未知用户）")}</td>
          <td>${esc(r.equipment_name)}<div style="color:var(--text-faint);font-size:11px">${esc(r.lab_label)}</div></td>
          <td class="mono">${esc(r.slot)}</td>
          <td><button class="btn sm" data-approve="${r.id}">通过</button>
              <button class="btn ghost sm" data-reject="${r.id}">驳回</button></td>
        </tr>`).join("")
      + '</tbody></table>';
    $$("[data-approve]",box).forEach(b=>b.addEventListener("click",()=>review(b,"approve")));
    $$("[data-reject]",box).forEach(b=>b.addEventListener("click",()=>review(b,"reject")));

    const picks = () => $$(".b-pick", box).filter(c=>c.checked).map(c=>Number(c.value));
    const syncHint = () => { $("#b-hint").textContent = "已选 " + picks().length + " 条"; };
    $$(".b-pick", box).forEach(c=>c.addEventListener("change", syncHint));
    $("#b-all", box).addEventListener("click", ()=>{ $$(".b-pick",box).forEach(c=>c.checked=true); syncHint(); });
    $("#b-none", box).addEventListener("click", ()=>{ $$(".b-pick",box).forEach(c=>c.checked=false); syncHint(); });
    $("#b-approve", box).addEventListener("click", ()=>batchReview(true));
    $("#b-reject", box).addEventListener("click", ()=>batchReview(false));
    stamp("#appr-box");
  }catch(e){ box.innerHTML='<div class="empty">加载失败：'+esc(e.message)+"</div>"; }
}

/* 批量审批（2026-09-29 加）。
   ⚠️ 结果要**逐条**呈现：失败项不是"环境坏了"，多半是"这条恰好在别人手里"，
   所以是"能批几条批几条 + 说清哪几条没批"，不是整批回滚。 */
async function batchReview(approve){
  const box = $("#appr-box");
  const ids = $$(".b-pick", box).filter(c=>c.checked).map(c=>Number(c.value));
  if(!ids.length){ alert("先勾选要处理的申请"); return; }
  let reason = "";
  if(!approve){
    reason = window.prompt("驳回理由（会记进审计，可留空）","") || "";
  }
  const verb = approve ? "通过" : "驳回";
  if(!confirm(`确认${verb}这 ${ids.length} 条申请？`)) return;
  const hint = $("#b-hint"); hint.textContent = "处理中…";
  try{
    const r = await api("/api/reservations/batch-review", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ids, approve, reason}),
    });
    let msg = `${verb}成功 ${r.approved} 条`;
    if(r.failed && r.failed.length){
      msg += `，失败 ${r.failed.length} 条：` + r.failed.map(f=>`#${f.id} ${f.reason}`).join("；");
    }
    hint.textContent = msg;
    if(r.failed && r.failed.length) alert(msg);
    await loadApprovals();
    toast && toast(msg);
  }catch(e){ hint.textContent = "失败：" + e.message; }
}

async function review(btn,kind){
  const id=btn.getAttribute(kind==="approve"?"data-approve":"data-reject");
  let body=null;
  if(kind==="reject"){
    // 用一个原生 prompt 而不是自建模态框：这里只有一个输入框，
    // 为它引一套组件不划算。理由会进驳回消息与审计。
    const reason=(window.prompt("驳回理由（会记进审计，可留空）","")||"");
    body=JSON.stringify({reason});
  }
  btn.disabled=true; const old=btn.textContent; btn.textContent="…";
  try{
    await api(`/api/reservations/${id}/${kind}`,{method:"POST",body});
    // 驳回会**当场释放时段**，所以刷新范围要包括预约记录与统计
    toast(kind==="approve"?("已通过 #"+id):("已驳回 #"+id),"ok");
    loadApprovals(); loadReservations(); loadHealth();
  }catch(e){ toast("操作失败："+e.message,"err"); btn.disabled=false; btn.textContent=old; }
}

const VIOL_TONE={};
async function loadViolations(){
  const box=$("#viol-box"), sel=$("#viol-user");
  fillUserSelect(sel);
  const uid=sel.value;
  if(!uid){ box.innerHTML='<div class="empty">选一个用户，看他的违约账</div>'; return; }
  box.innerHTML=skel(2,"row");
  try{
    const v=await api(`/api/users/${encodeURIComponent(uid)}/violations`);
    const state0=v.blocked?"已被限制":(v.over_threshold?"已超阈值":"正常");
    const tone=v.blocked?"bad":(v.over_threshold?"warn":"");
    box.innerHTML=`<div class="hint" style="margin-bottom:10px">
        <span class="tag ${tone}">${esc(state0)}</span> ${esc(v.message||"")}</div>
      <table><thead><tr><th>项目</th><th>值</th></tr></thead><tbody>
        <tr><td>窗口内计入次数</td><td class="mono">${v.count}</td></tr>
        <tr><td>阈值</td><td class="mono">${v.threshold}</td></tr>
        <tr><td>窗口天数</td><td class="mono">${v.window_days}</td></tr>
        <tr><td>限制是否启用</td><td>${v.blocking_enabled?"是":"否（默认只记不罚）"}</td></tr>
      </tbody></table>`;

    // 判定记录：**能看到证据才给按钮**。
    // 豁免这个动作必须在能看到"系统凭什么判的"的地方做 ——
    // 否则管理员只是在盲点一个按钮。
    const rows=await api(`/api/reservations?user_id=${encodeURIComponent(uid)}`);
    const marked=rows.filter(r=>r.no_show_at);
    if(!marked.length){
      box.innerHTML+='<div class="empty" style="margin-top:12px">没有被判定过「未到场」的预约</div>';
      return;
    }
    box.innerHTML+='<div class="hint" style="margin:12px 0 6px">判定记录（豁免会保留判定本身，只标记「已推翻」）</div>'
      +'<table><thead><tr><th>ID</th><th>时段</th><th>判定时间</th><th></th></tr></thead><tbody>'
      + marked.map(r=>`<tr>
          <td class="mono">#${r.id}</td><td class="mono">${esc(r.slot)}</td>
          <td class="mono">${esc(fmtStamp(r.no_show_at))}</td>
          <td>${r.pardoned_at
            ? '<span class="tag ok">已豁免 '+esc(fmtStamp(r.pardoned_at))+"</span>"
            : `<button class="btn ghost sm" data-pardon="${r.id}">豁免</button>`}</td>
        </tr>`).join("")
      +'</tbody></table>';
    $$("[data-pardon]",box).forEach(b=>b.addEventListener("click",async()=>{
      const id=b.getAttribute("data-pardon");
      // 豁免接口**不收理由**，所以这里不去假装收集一个 —— 收一个不会被记录的
      // 理由，比不收更糟：它会让管理员以为"我写过原因了"。
      if(!window.confirm("确认推翻 #"+id+" 的未到场判定？判定本身会保留（标记为已豁免）。")) return;
      b.disabled=true;
      try{
        await api(`/api/reservations/${id}/pardon`,{method:"POST"});
        toast("已豁免 #"+id,"ok"); loadViolations(); loadHealth();
      }catch(e){ toast("豁免失败："+e.message,"err"); b.disabled=false; }
    }));
  }catch(e){ box.innerHTML='<div class="empty">加载失败：'+esc(e.message)+"</div>"; }
}

const NOTIFY_CN={pending:"待投递",sent:"已投递",skipped:"已跳过",failed:"投递失败"};
async function loadNotifications(){
  const box=$("#notif-box"), sel=$("#notif-user");
  fillUserSelect(sel);
  const uid=sel.value;
  if(!uid){ box.innerHTML='<div class="empty">选一个用户看他的通知</div>'; return; }
  box.innerHTML=skel(2,"row");
  try{
    // 管理员看别人的通知必须显式带上 user_id（接口默认只返回本人的）
    const rows=await api(`/api/notifications?user_id=${encodeURIComponent(uid)}`);
    if(!rows.length){ box.innerHTML='<div class="empty">这个人没有通知</div>'; return; }
    const dist={}; rows.forEach(n=>{dist[n.status]=(dist[n.status]||0)+1;});
    box.innerHTML=`<div class="hint" style="margin-bottom:10px">共 ${rows.length} 条 · `
      + Object.keys(dist).map(k=>`${esc(NOTIFY_CN[k]||k)} ${dist[k]}`).join(" · ")
      + `</div><table><thead><tr><th>时间</th><th>类型</th><th>标题</th><th>状态</th></tr></thead><tbody>`
      + rows.map(n=>`<tr>
          <td class="mono">${esc(fmtStamp(n.created_at))}</td>
          <td>${esc(n.kind)}</td>
          <td>${esc(n.title)}<div style="color:var(--text-faint);font-size:11px">${esc(n.body)}</div></td>
          <td><span class="tag ${n.status==="failed"?"bad":(n.status==="sent"?"ok":"warn")}">${esc(NOTIFY_CN[n.status]||n.status)}</span></td>
        </tr>`).join("")
      + '</tbody></table>';
  }catch(e){ box.innerHTML='<div class="empty">加载失败：'+esc(e.message)+"</div>"; }
}

async function loadAudit(){
  const box=$("#audit-box");
  box.innerHTML=skel(3,"row");
  const action=$("#audit-action").value.trim();
  try{
    const rows=await api("/api/audit?limit=100"+(action?("&action="+encodeURIComponent(action)):""));
    if(!rows.length){ box.innerHTML='<div class="empty">没有匹配的审计记录</div>'; return; }
    box.innerHTML='<table><thead><tr><th>时间</th><th>动作</th><th>操作者</th><th>对象</th><th>结果</th></tr></thead><tbody>'
      + rows.map(a=>`<tr>
          <td class="mono">${esc(fmtStamp(a.created_at))}</td>
          <td class="mono">${esc(a.action)}</td>
          <td>${esc(a.actor_name||"—")}</td>
          <td class="mono">${esc(a.target_type)}${a.target_id?("#"+esc(a.target_id)):""}</td>
          <td><span class="tag ${a.outcome==="ok"?"ok":"bad"}">${esc(a.outcome)}</span>
            <div style="color:var(--text-faint);font-size:11px">${esc(a.detail)}</div></td>
        </tr>`).join("")
      + '</tbody></table>';
  }catch(e){ box.innerHTML='<div class="empty">加载失败：'+esc(e.message)+"</div>"; }
}

/* ---------- 直接预约（不经过模型） ----------
   这一块存在的理由写在 P0-3 的评估里：模型挂掉时，如果预约的唯一入口
   是"跟模型说一句话"，那整个预约功能就一起挂了。 */
const BK_TIMES=(function(){
  const out=[];
  for(let m=8*60;m<=22*60;m+=30){
    const h=String(Math.floor(m/60)).padStart(2,"0"), mm=String(m%60).padStart(2,"0");
    out.push(h+":"+mm);
  }
  return out;
})();

function fillBookingOptions(){
  const sel=$("#bk-equip");
  if(sel.options.length) return;   // 已填过就别重建，否则会丢掉用户当前选择
  const items=Array.from(state.equipment.values());
  if(!items.length){ sel.innerHTML='<option value="">（设备目录尚未加载）</option>'; return; }
  sel.innerHTML=items.map(e=>{
    const off = e.status && e.status!=="normal";
    const tag = off ? "（"+esc(e.status)+"）" : (e.requires_approval?"（需审批）":"");
    return `<option value="${e.id}"${off?" disabled":""}>${esc(e.name)} · ${esc(e.code)}${tag}</option>`;
  }).join("");
  const start=$("#bk-start"), end=$("#bk-end");
  start.innerHTML=BK_TIMES.map(t=>`<option value="${t}"${t==="10:00"?" selected":""}>${t}</option>`).join("");
  end.innerHTML=BK_TIMES.map(t=>`<option value="${t}"${t==="12:00"?" selected":""}>${t}</option>`).join("");
  if(!$("#bk-date").value) $("#bk-date").value = isoDate(1);
}

function isoDate(offsetDays){
  const d=new Date(); d.setDate(d.getDate()+(offsetDays||0));
  return d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0");
}

async function submitBooking(){
  const box=$("#bk-result"), btn=$("#bk-go");
  const equipmentId=Number($("#bk-equip").value);
  const date=$("#bk-date").value, start=$("#bk-start").value, end=$("#bk-end").value;
  if(!equipmentId){ box.innerHTML='<div class="empty">先选一台设备</div>'; return; }
  if(!date){ box.innerHTML='<div class="empty">先选日期</div>'; return; }
  if(start>=end){ box.innerHTML='<div class="empty">结束时间要晚于开始时间</div>'; return; }

  btn.disabled=true; btn.textContent="提交中…";
  box.innerHTML='<div class="skel sk-row"></div>';
  try{
    // 返回的是 BookingOutcome：真正的预约在 .reservation 里，
    // 顶层只有 ok / message / reason（reason 是机器可读的分类，给人看的在 message）。
    const r=await api("/api/reservations",{method:"POST",body:JSON.stringify({
      equipment_id:equipmentId, date:date, start:start, end:end,
      purpose:$("#bk-purpose").value.trim()})});
    const res=r.reservation;
    const needReview = !!res && res.status==="pending";
    box.innerHTML=`<div class="empty">${needReview?"已提交，等待管理员审批":"预约成功"}
      — ${res?("#"+res.id+" · "+esc(res.slot)):esc(r.message)}</div>`;
    toast(needReview?"已提交申请":"预约成功","ok");
    loadReservations(); loadHealth();
  }catch(e){
    // ★ 失败信息要**原样**显示服务端给的原因，不要自己翻译成"预约失败"。
    // 那些原因码是给用户的（时段冲突 / 资质不够 / 违约被限 / 超出单次上限），
    // 翻译成一句笼统的话，用户就只剩下"换个时间再试试"这一条路可走。
    const hint = e.status===409 ? "换个时段再试"
      : e.status===403 ? "这不是时段问题，是资格问题（资质 / 违约限制）"
      : e.status===422 ? "填的内容有问题" : "";
    box.innerHTML=`<div class="empty">预约失败：${esc(e.message)}${hint?"<br>"+esc(hint):""}</div>`;
    toast("预约失败："+e.message,"err");
  }finally{ btn.disabled=false; btn.textContent="提交预约"; }
}

$("#bk-go").addEventListener("click",submitBooking);
$("#bk-tmr").addEventListener("click",()=>{
  $("#bk-date").value=isoDate(1);
  $("#bk-start").value="10:00"; $("#bk-end").value="12:00";
  toast("已填好明天 10:00–12:00");
});

/* ---------- 实验室与设备 ---------- */
async function loadLabs(){
  const box=$("#labs-box");
  box.innerHTML=skel(3,"card");
  try{
    const labs=await api("/api/labs");
    labs.forEach(l=>l.equipment.forEach(e=>state.equipment.set(e.id,e)));
    box.innerHTML=labs.map(l=>{
      const rows=l.equipment.map(e=>{
        const st=e.status==="normal"
          ? '<span class="tag ok">可用</span>'
          : `<span class="tag bad">${esc(e.status)}</span>`;
        const cert=e.requires_training?'<span class="tag warn">需资质</span>':'<span class="tag">免资质</span>';
        return `<tr><td class="mono">${esc(e.code)}</td><td>${esc(e.name)}</td><td>${esc(e.category)}</td>
          <td class="mono">${e.max_hours}h</td><td>${cert}</td><td>${st}</td></tr>`;
      }).join("");
      const oh=l.open_hours||{};
      const fmt=v=>v?`${v[0]}-${v[1]}`:"—";
      return `<div style="margin-bottom:20px">
        <div style="display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;margin-bottom:8px">
          <b style="font-size:14px">${esc(l.label)}</b>
          <span class="tag">容纳 ${l.capacity} 人</span>
          <span class="tag">工作日 ${esc(fmt(oh.weekday))}</span>
          <span class="tag">周末 ${esc(fmt(oh.weekend))}</span>
        </div>
        <div style="color:var(--text-faint);font-size:12px;margin-bottom:8px">${esc(l.note||"")}</div>
        <table><thead><tr><th>编号</th><th>名称</th><th>类别</th><th>单次上限</th><th>准入</th><th>状态</th></tr></thead>
        <tbody>${rows}</tbody></table>
      </div>`;
    }).join("");
  }catch(e){box.innerHTML='<div class="empty">加载失败：'+esc(e.message)+"</div>";}
}

/* ---------- 预约记录 ---------- */
const STATUS_TAG={pending:"warn",confirmed:"ok",cancelled:"",completed:"ok",expired:"bad"};
const STATUS_CN={pending:"待确认",confirmed:"已确认",cancelled:"已取消",completed:"已完成",expired:"已过期"};

async function loadReservations(){
  const box=$("#res-box");
  box.innerHTML=skel(3,"row");
  const uid=$("#flt-user").value, st=$("#flt-status").value;
  try{
    let rows=await api("/api/reservations"+(uid?("?user_id="+encodeURIComponent(uid)):""));
    if(st) rows=rows.filter(r=>r.status===st);
    if(!rows.length){box.innerHTML='<div class="empty">没有匹配的预约记录</div>';return;}

    const total=rows.length;
    const dist={}; rows.forEach(r=>{dist[r.status]=(dist[r.status]||0)+1;});
    const colors={pending:"var(--warn)",confirmed:"var(--ok)",cancelled:"var(--text-faint)",completed:"var(--accent)",expired:"var(--danger)"};
    const bar=Object.keys(dist).map(k=>
      `<div class="seg" style="width:${(dist[k]/total*100).toFixed(1)}%;background:${colors[k]||"var(--border)"}" title="${esc(STATUS_CN[k]||k)} ${dist[k]}"></div>`).join("");

    const table=rows.map(r=>{
      const canCancel=r.status==="pending"||r.status==="confirmed";
      return `<tr>
        <td class="mono">#${r.id}</td>
        <td>${esc(r.equipment_name)}<div style="color:var(--text-faint);font-size:11px">${esc(r.lab_label)}</div></td>
        <td class="mono">${esc(r.slot)}</td>
        <td><span class="tag ${STATUS_TAG[r.status]||""}">${esc(STATUS_CN[r.status]||r.status)}</span></td>
        <td>${canCancel
          ? `<button class="btn ghost sm" data-move="${r.id}">改期</button>`
            + `<button class="btn ghost sm" data-cancel="${r.id}">取消</button>`
          : ""}</td>
      </tr>`;
    }).join("");

    box.innerHTML=`<div class="sq"><span class="lb">分布</span><span class="track">${bar}</span></div>
      <table style="margin-top:10px"><thead><tr><th>ID</th><th>设备</th><th>时段</th><th>状态</th><th></th></tr></thead>
      <tbody>${table}</tbody></table>`;

    // 改期：走 PATCH（**一个事务**里换坑），不是"取消再重约"。
    // 后者在新旧时段之间有个窗口，别人一抢就两头落空 —— 界面上必须给出
    // 真正的改期入口，否则用户只能自己那么干。
    $$("[data-move]",box).forEach(b=>b.addEventListener("click",()=>{
      const id=b.getAttribute("data-move");
      const row=rows.find(x=>String(x.id)===String(id));
      const raw=window.prompt(
        "新的时段（HH:MM-HH:MM）", (row&&row.slot?String(row.slot).slice(-13):"")
      );
      if(!raw) return;
      const parts=String(raw).split(/[-–—～~]/).map(s=>s.trim());
      if(parts.length!==2 || !/^\d{2}:\d{2}$/.test(parts[0]) || !/^\d{2}:\d{2}$/.test(parts[1])){
        toast("格式不对，请写成 14:00-16:00","err"); return;
      }
      b.disabled=true; const old=b.textContent; b.textContent="…";
      api(`/api/reservations/${id}`,{method:"PATCH",body:JSON.stringify({
        start: parts[0]+":00", end: parts[1]+":00"})})
        .then(out=>{ toast(out.message||("已改期 #"+id),"ok"); loadReservations(); loadHealth(); })
        .catch(e=>{ toast("改期失败："+e.message,"err"); b.disabled=false; b.textContent=old; });
    }));
    $$("[data-cancel]",box).forEach(b=>b.addEventListener("click",async()=>{
      const id=b.getAttribute("data-cancel");
      b.disabled=true; b.textContent="…";
      try{
        // 身份不再由前端指定：取消者取自令牌。
        // 管理员要代他人取消得用 as_user_id，普通用户传了会 403。
        await api("/api/reservations/cancel",{method:"POST",body:JSON.stringify({
          reservation_id:Number(id), reason:"控制台取消"})});
        toast("已取消预约 #"+id,"ok");
        loadReservations(); loadHealth();
      }catch(e){ toast("取消失败："+e.message,"err"); b.disabled=false; b.textContent="取消"; }
    }));
  }catch(e){box.innerHTML='<div class="empty">加载失败：'+esc(e.message)+"</div>";}
}

/* ---------- 规范检索 ---------- */
async function runRetrieve(){
  const q=$("#inp-q").value.trim();
  if(!q){toast("先输入检索词","err");return;}
  const box=$("#kb-box");
  box.innerHTML=skel(3,"row");
  try{
    const r=await api("/api/retrieve?q="+encodeURIComponent(q)+"&k=4");
    if(!r.hits.length){box.innerHTML='<div class="empty">没有命中</div>';return;}
    box.innerHTML=`<div class="hint">后端 ${esc(r.backend)} · 语料 ${r.stats.chunks||"-"} 条${r.degraded_reason?" · 降级原因："+esc(r.degraded_reason):""}</div>`
      + r.hits.map(h=>{
        const by=(h.matched_by||[]).length?` <span class="tag">${esc(h.matched_by.join("+"))}</span>`:"";
        return `<div class="cite"><div class="h">${esc(h.heading)}${by}</div>
          <div class="x">${esc(h.text.slice(0,150))}${h.text.length>150?"…":""}</div>
          <div class="s">score ${h.score}</div></div>`;
      }).join("");
  }catch(e){box.innerHTML='<div class="empty">检索失败：'+esc(e.message)+"</div>";}
}

/* ---------- 对话 ---------- */
function pushMsg(role,html,kind){
  const el=document.createElement("div");
  el.className="msg "+role+(kind?(" "+kind):"");
  el.innerHTML=`<div class="who">${role==="u"?"我":"AI"}</div><div class="bub">${html}</div>`;
  $("#msgs").appendChild(el);
  $("#msgs").scrollTop=$("#msgs").scrollHeight;
  return el;
}

/* 停止生成（2026-09-29 加）：中断当前这次请求。
   用 AbortController 而不是"把按钮禁掉" —— 后者只是不让人点，
   请求还在跑、配额还在消耗。 */
$("#btn-stop").addEventListener("click", () => {
  if(state.abort){
    state.abort.abort();
  }
});
function skeletonMsg(){
  return pushMsg("a",'<div class="skel" style="height:14px;width:70%"></div><div class="skel" style="height:14px;width:45%;margin-top:7px"></div>');
}
function renderProposals(list){
  if(!list||!list.length) return "";
  return '<div class="props">'+list.map((p,i)=>{
    const relax=(p.relaxations||[]).length
      ? '<div class="relax">'+p.relaxations.map(r=>`<span class="rc">${esc(r)}</span>`).join("")+"</div>"
      : '<div class="relax"><span class="rc" style="background:color-mix(in srgb,var(--ok) 14%,transparent);color:var(--ok);border-color:color-mix(in srgb,var(--ok) 30%,transparent)">完全满足</span></div>';
    return `<div class="prop"><span class="idx">${i+1}</span><div class="main">
      <div class="line1">${esc(p.equipment_name)}<span style="color:var(--text-faint);font-weight:400"> · ${esc(p.lab_label)}</span></div>
      <div class="line2">${esc(p.date)} ${esc(p.start)}-${esc(p.end)} · ${p.hours}h</div>
      ${relax}</div>
      <div style="display:flex;flex-direction:column;gap:6px;align-items:flex-end">
        <span class="score">${p.score}</span>
        <button class="btn sm" data-accept="${i}">选这个</button>
      </div></div>`;
  }).join("")+"</div>";
}
function renderTrace(trace){
  if(!trace||!trace.length) return "";
  const max=Math.max.apply(null,trace.map(t=>t.elapsed_ms||0))||1;
  return '<div class="trace"><div style="font-size:11px;color:var(--text-faint);margin-bottom:6px">执行轨迹</div><div class="tl">'
    + trace.map(t=>`<div class="tlrow"><span class="n">${esc(t.node)}</span>
        <span><span class="bar" style="width:${Math.max(2,(t.elapsed_ms/max)*100).toFixed(1)}%;display:inline-block"></span>
        <span class="d" style="margin-left:6px">${esc(t.detail)}</span></span>
        <span class="m">${t.elapsed_ms}ms</span></div>`).join("")
    + "</div></div>";
}
function renderCitations(list){
  if(!list||!list.length) return "";
  return '<div style="margin-top:10px">'+list.map(h=>
    `<div class="cite"><div class="h">${esc(h.heading)}</div>
     <div class="x">${esc(h.text.slice(0,160))}${h.text.length>160?"…":""}</div>
     <div class="s">${esc(h.source)} · score ${h.score}</div></div>`).join("")+"</div>";
}

let lastProposals=[];
/* ---- 对话流式（2026-09-29 加）----
   原对话是"等整段返回"：一次要走 parse → negotiate → book → compose 好几个节点，
   前面几秒界面上只有转圈。流式之后每走完一个节点就多一行进度 ——
   **总耗时没变，但等待变成了可见的进展**。

   为什么不用 EventSource：它只支持 GET，把用户那句话塞进查询串既受长度限制
   （中文还要编码）、又会进访问日志。这里用 fetch + ReadableStream 逐块读 SSE，
   消息体照旧走 body，还能用 AbortController 做"停止生成"。 */
async function chatStream(payload, onNode, signal){
  const headers = {"Content-Type":"application/json"};
  const t = tokenApi.get();
  if(t) headers.Authorization = "Bearer " + t;

  const r = await fetch("/api/agent/chat/stream", {
    method:"POST", headers, body: JSON.stringify(payload), signal,
  });
  if(r.status === 401){ sessionExpired(); throw new Error("登录状态已失效，请重新登录"); }
  if(!r.ok){ throw new Error("Agent 执行失败：HTTP " + r.status); }

  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "", done = null;
  while(true){
    const { value, done: fin } = await reader.read();
    if(fin) break;
    buf += dec.decode(value, { stream:true });
    // SSE 以空行分帧；最后一段可能不完整，留在 buf 里等下一块
    const parts = buf.split("\n\n");
    buf = parts.pop();
    for(const part of parts){
      const line = part.split("\n").find(l => l.startsWith("data: "));
      if(!line) continue;
      let ev; try{ ev = JSON.parse(line.slice(6)); }catch(_){ continue; }
      if(ev.type === "node") onNode && onNode(ev);
      else if(ev.type === "done") done = ev.data;
      else if(ev.type === "error") throw new Error(ev.detail);
    }
  }
  if(!done) throw new Error("流意外中断（没收到终态）");
  return done;
}

async function send(message,accept){
  if(state.busy) return;
  const text=(message||"").trim();
  if(!text && !accept){toast("说点什么再发","err");return;}
  state.busy=true; $("#btn-send").disabled=true; $("#hint-inline").textContent="";
  if(text) pushMsg("u",esc(text));
  $("#inp-msg").value="";
  const ph=skeletonMsg();

  const payload={message:text||"就选这个",session_id:state.session};
  if(accept){
    payload.accept_equipment_id=accept.equipment_id;
    payload.accept_date=accept.date;
    payload.accept_start=accept.start;
    payload.accept_end=accept.end;
  }

  // 停止生成：只对**本次**对话生效，中断后把 busy 复位，不会把界面卡死
  const ctl = new AbortController();
  state.abort = ctl;
  const stopBtn = $("#btn-stop");
  if(stopBtn) stopBtn.hidden = false;

  try{
    let lastNode = "";
    const r = await chatStream(payload, ev => {
      // 节点进度直接写进占位气泡里 —— 这是"等待可见"的全部意义
      lastNode = ev.node || lastNode;
      ph.querySelector(".bub").innerHTML = '<span class="dots"><i></i></span> '
        + "<span class='mono' style='font-size:12px'>"
        + esc(NODE_LABEL[ev.node] || ev.node || "处理中")
        + (ev.elapsed_ms ? " · " + Math.round(ev.elapsed_ms) + "ms" : "")
        + "</span>";
    }, ctl.signal);

    ph.remove();
    lastProposals=r.proposals||[];
    let html=esc(r.reply);
    if(r.booking&&r.booking.reservation){
      const b=r.booking;
      html+='<div style="margin-top:8px"><span class="tag '+(b.ok?"ok":"bad")+'">'
        + (b.ok?"已下单":"下单失败")+"</span> <span class='mono'>"
        + esc(b.reservation.slot||"")+"</span>"
        + (b.retries?` <span class="tag warn">重试 ${b.retries} 次</span>`:"")+"</div>";
    }else if(r.booking&&!r.booking.ok){
      html+='<div style="margin-top:8px"><span class="tag bad">'+esc(r.booking.message)+"</span></div>";
    }
    if(r.degraded) html+='<div style="margin-top:8px"><span class="tag warn">降级模式</span></div>';
    html+=renderProposals(r.proposals);
    html+=renderCitations(r.citations);
    html+=renderTrace(r.trace);
    const el=pushMsg("a",html);
    $$("[data-accept]",el).forEach(btn=>btn.addEventListener("click",()=>{
      const p=lastProposals[Number(btn.getAttribute("data-accept"))];
      if(p) send("选：" + p.equipment_name + " " + p.date + " " + p.start + "-" + p.end, p);
    }));
    if(r.booking&&r.booking.ok){ loadReservations(); loadHealth(); }
  }catch(e){
    ph.remove();
    // 用户主动停止不是"出错"：说清楚状态，别让他以为系统坏了
    if(e && e.name === "AbortError"){
      pushMsg("a", esc("已停止生成。你可以换个说法再问一次，或者直接用上面的表单下单。"), "err");
    }else{
      pushMsg("a",esc("出错了："+e.message),"err");
      toast(e.message,"err");
    }
  }finally{
    state.busy=false; $("#btn-send").disabled=false;
    state.abort = null;
    if(stopBtn) stopBtn.hidden = true;
  }
}

/* 节点名 → 人话。图里的节点名是工程术语（parse/negotiate/book），
   直接显示出来使用者看不懂，而进度条看不懂就等于没有。 */
const NODE_LABEL = {
  parse: "正在理解你的需求…",
  ask: "还缺几个条件，正在准备追问…",
  negotiate: "正在查可用时段与设备…",
  book: "正在下单…",
  cancel: "正在处理取消…",
  retrieve: "正在查规范文档…",
  compose: "正在组织回复…",
  degrade: "模型不可用，切到引导式表单…",
};

/* ---------- 交互绑定 ---------- */
const QUICK=["明天下午两点想用荧光光谱仪两小时","明天上午十点到十一点，紫外可见分光光度计",
  "离心机使用有什么安全规范","我想约个设备"];
$("#quick").innerHTML=QUICK.map(q=>`<button class="btn ghost sm" data-q="${esc(q)}">${esc(q)}</button>`).join("");
$$("[data-q]").forEach(b=>b.addEventListener("click",()=>{
  $("#inp-msg").value=b.getAttribute("data-q");
  $("#hint-inline").textContent="";
  send($("#inp-msg").value);
}));

$("#btn-send").addEventListener("click",()=>send($("#inp-msg").value));
$("#inp-msg").addEventListener("keydown",e=>{
  if(e.key==="Enter"&&(e.ctrlKey||e.metaKey)){e.preventDefault();send($("#inp-msg").value);}
});
$("#inp-msg").addEventListener("input",()=>{
  const v=$("#inp-msg").value;
  $("#hint-inline").textContent=v.length>2000?("超出长度上限（"+v.length+"/2000）"):"Ctrl + Enter 发送";
  $("#btn-send").disabled=v.length>2000||state.busy;
});
$("#btn-reset").addEventListener("click",()=>{
  state.session="web-"+Math.random().toString(36).slice(2,8);
  $("#msgs").innerHTML="";
  pushMsg("a","已开新会话。上一轮的备选方案不会再被引用。");
});
$("#flt-user").addEventListener("change",loadReservations);
$("#flt-status").addEventListener("change",loadReservations);
$("#btn-q").addEventListener("click",runRetrieve);
$("#inp-q").addEventListener("keydown",e=>{if(e.key==="Enter")runRetrieve();});

/* ---------- 登录闸门交互 ---------- */
$("#lg-go").addEventListener("click",doLogin);
$("#lg-pass").addEventListener("keydown",e=>{if(e.key==="Enter")doLogin();});
$("#lg-user").addEventListener("keydown",e=>{if(e.key==="Enter")$("#lg-pass").focus();});
$$("[data-demo]").forEach(b=>b.addEventListener("click",()=>{
  const [u,p]=b.getAttribute("data-demo").split("|");
  $("#lg-user").value=u; $("#lg-pass").value=p;
  gateError("");
  $("#lg-go").focus();
}));
$("#btn-logout").addEventListener("click",()=>{
  clearSession();
  showGate();
  toast("已退出登录");
  loadHealth();
});

// 面板名 → 切过去时要拉什么。写成表而不是一串 if：
// 加一个标签页只改这张表，不会漏掉"新面板忘了加载"这种错。
const PANES=["book","labs","res","kb","users","appr","viol","notif","audit"];
const TAB_LOADERS={
  book:fillBookingOptions, labs:loadLabs, res:loadReservations, kb:null,
  users:loadUserDirectory, appr:loadApprovals, viol:loadViolations,
  notif:loadNotifications, audit:loadAudit,
};
$$("#tabs .tab").forEach(tab=>tab.addEventListener("click",()=>{
  $$("#tabs .tab").forEach(t=>t.classList.toggle("on",t===tab));
  const name=tab.getAttribute("data-tab");
  PANES.forEach(k=>{$("#pane-"+k).hidden = k!==name;});
  const load=TAB_LOADERS[name];
  if(load) load();
}));
$("#viol-refresh") && $("#viol-refresh").addEventListener("click",loadViolations);
$("#notif-refresh") && $("#notif-refresh").addEventListener("click",loadNotifications);
$("#audit-refresh") && $("#audit-refresh").addEventListener("click",loadAudit);
$("#audit-action") && $("#audit-action").addEventListener("keydown",e=>{ if(e.key==="Enter") loadAudit(); });

/* ---------- 启动 ----------
   健康检查是公开端点，先跑，好让登录页顶部也能显示后端连接状态；
   令牌存在就试着用它换身份，能换到就直接进主界面。 */
(async function boot(){
  await loadHealth();
  if(!tokenApi.get()){ showGate(); return; }
  try{
    state.me=await api("/api/auth/me");
    $("#gate").hidden=true;
    await enterApp();
  }catch(e){
    // api() 遇到 401 已调 sessionExpired()，闸门已经在了
  }
})();
})();
