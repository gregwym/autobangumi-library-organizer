const css=`:host{display:block;font:14px/1.5 var(--ab-font-family,system-ui);color:var(--ab-color-text,#222)}*{box-sizing:border-box}details{border:1px solid var(--ab-color-border,#ddd);border-radius:var(--ab-radius-md,8px);overflow:visible}summary{padding:7px 14px;font-weight:600;cursor:pointer;color:var(--ab-color-text-secondary,#666)}.body{padding:14px;background:var(--ab-color-surface,#fff);border-top:1px solid var(--ab-color-border,#ddd)}.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:10px 0}input,select,button,textarea{font:inherit;color:inherit;background:transparent;border:1px solid var(--ab-color-border,#aaa);border-radius:6px;padding:7px 10px}button{cursor:pointer}button:disabled{opacity:.5;cursor:default}.primary{background:var(--ab-color-primary,#5274d9);color:white}label{display:grid;gap:5px;margin:8px 0}label:has(input[type=checkbox]){display:flex;align-items:center;gap:8px}input[type=checkbox]{width:auto;margin:0;accent-color:var(--ab-color-primary,#526fe4)}select,textarea{max-width:100%;width:100%}textarea{min-height:70px}small,.muted{color:var(--ab-color-text-secondary,#777)}.plan{max-height:400px;overflow:auto}.item{border-bottom:1px solid var(--ab-color-border,#ddd);padding:9px 0;overflow-wrap:anywhere}#message{white-space:pre-wrap;margin-top:10px}.badge{border-radius:999px;padding:2px 9px;background:#8882}.filter{border-radius:999px;padding:4px 10px}.filter[aria-pressed=true],.badge.enabled{background:var(--ab-color-primary,#5274d9);color:white;border-color:var(--ab-color-primary,#5274d9)}.help{position:relative;display:inline-flex;vertical-align:middle;font-weight:400}.info{border:0;padding:0;width:22px;height:22px;border-radius:50%;cursor:help;color:var(--ab-color-text-secondary,#777);line-height:1}.info:focus-visible{outline:2px solid var(--ab-color-primary,#5274d9);outline-offset:2px}.tooltip{display:none;position:absolute;z-index:30;top:calc(100% + 8px);left:0;width:min(360px,calc(100vw - 32px));max-height:70vh;overflow:auto;padding:10px 12px;border:1px solid var(--ab-color-border,#ddd);border-radius:8px;background:var(--ab-color-surface,#fff);color:var(--ab-color-text,#222);font:13px/1.6 var(--ab-font-family,system-ui);box-shadow:0 4px 16px #0002;white-space:normal;text-align:left;cursor:auto}.help:hover .tooltip,.help:focus-within .tooltip,.help.open .tooltip{display:block}.help.dismissed .tooltip{display:none}.error{color:#c65540}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;vertical-align:top;padding:9px 10px;border-bottom:1px solid var(--ab-color-border,#ddd)}th{position:sticky;top:0;background:var(--ab-color-surface,#fff)}td.path{min-width:230px;max-width:340px;overflow-wrap:anywhere;white-space:pre-wrap}td{min-width:90px}.table-scroll{overflow:auto;max-height:400px}[hidden]{display:none!important}`;
const progressMarkup = `<section id="progress-panel" hidden aria-live="polite"><p id="progress-label"></p><progress aria-label="文件整理进度" id="progress-bar" max="1" value="0" style="width:100%;accent-color:var(--ab-color-primary,#5274d9)"></progress><p id="progress-current" class="muted" style="overflow-wrap:anywhere"></p><small id="progress-counts"></small></section>`;
const operationNames={unchanged:'已有目标 · 不变',reuse:'复用已有副本',adopt:'接管旧文件 · 改名',adopt_existing:'接管已有目标',create:'新建目标',held:'待确认',skipped:'已跳过'};
function info(id,text) {
  return `<span class="help"><button type="button" class="info" aria-label="查看说明" aria-describedby="help-${id}">ⓘ</button><span id="help-${id}" class="tooltip" role="tooltip">${text}</span></span>`;
}
const planMarkup=`<section id="review" hidden><div class="row"><span id="plan-summary"></span>${info('plan','筛选仅影响显示，执行仍应用完整计划。预览有效期 15 分钟；文件、配置或映射变化后须重新预览。待确认项目保持原位。')}</div><div id="plan-filters" class="row" role="group" aria-label="按预期操作筛选"></div><label>筛选预览<input id="plan-filter" type="search" placeholder="番名、路径或操作"></label><div id="plan" class="table-scroll"></div><div class="row"><button id="plan-prev">上一页</button><span id="plan-page"></span><button id="plan-next">下一页</button></div></section>`;
const stateNames = {linked:'已整理',exists:'已有链接',held:'待确认',failed:'失败',skipped:'已跳过'};
function summary(rows) {
  const counts={}; for(const r of rows){const key=r.action==='relocated'?'复用副本':r.state==='linked'&&r.method?({hardlink:'硬链',reflink:'Reflink',copy:'普通复制'}[r.method]||r.state):r.state;counts[key]=(counts[key]||0)+1;}
  return Object.entries(counts).map(([key,n])=>`${stateNames[key]||key} ${n}`).join(' · ');
}
class Panel extends HTMLElement {
  mount(body) {
    if(!this.shadowRoot)this.attachShadow({mode:'open'});
    this.shadowRoot.innerHTML=`<style>${css}</style>${body}`;
    this.$=id=>this.shadowRoot.getElementById(id);
    for(const help of this.shadowRoot.querySelectorAll('.help')) {
      const button=help.querySelector('button'),tip=help.querySelector('.tooltip');
      const place=()=>{
        tip.style.left='0';tip.style.top='calc(100% + 8px)';tip.style.bottom='auto';
        const rect=tip.getBoundingClientRect();
        tip.style.left=Math.max(16-rect.left,Math.min(0,window.innerWidth-16-rect.right))+'px';
        if(rect.bottom>window.innerHeight-16&&rect.height<help.getBoundingClientRect().top-16){tip.style.top='auto';tip.style.bottom='calc(100% + 8px)';}
      };
      help.onmouseenter=place;
      help.onmouseleave=()=>help.classList.remove('dismissed');
      button.onfocus=()=>{help.classList.remove('dismissed');place();};
      button.onblur=()=>help.classList.remove('open');
      button.onclick=event=>{
        event.preventDefault();help.classList.remove('dismissed');help.classList.toggle('open');button.focus({preventScroll:true});place();
      };
      button.onkeydown=event=>{if(event.key==='Escape'){help.classList.remove('open');help.classList.add('dismissed');event.stopPropagation();}};
    }
  }
  initPlan() {
    this.$('plan-filter').oninput=()=>{this.planPage=0;this.renderPlanPage();};
    this.action('plan-prev',()=>{this.planPage--;this.renderPlanPage();});
    this.action('plan-next',()=>{this.planPage++;this.renderPlanPage();});
  }
  renderPlan(plan) {
    this.planRows=plan.items;this.planPage=0;this.planOperation='';this.$('plan-filter').value='';
    this.$('review').hidden=false;
    this.$('plan-summary').textContent=`完整预览：${plan.items.length} 个文件`;
    this.$('plan-filters').replaceChildren(...[['',plan.items.length],...Object.entries(plan.counts||{})].map(([operation,count])=>{
      const button=document.createElement('button');button.type='button';button.className='filter';button.dataset.operation=operation;
      button.textContent=`${operationNames[operation]||'全部'} ${count}`;
      button.onclick=()=>{if(this.busy)return;this.planOperation=operation;this.planPage=0;this.renderPlanPage();};
      return button;
    }));
    this.renderPlanPage();
  }
  filteredPlan() {
    const q=this.$('plan-filter').value.trim().toLowerCase();
    return (this.planRows||[]).filter(i=>(!this.planOperation||i.operation===this.planOperation)&&(!q||JSON.stringify(i).toLowerCase().includes(q)||(operationNames[i.operation]||'').includes(q)));
  }
  syncPlanPagination() {
    this.$('plan-prev').disabled=!this.planPage;
    this.$('plan-next').disabled=(this.planPage+1)*25>=this.filteredPlan().length;
  }
  renderPlanPage() {
    for(const button of this.$('plan-filters').querySelectorAll('button'))button.setAttribute('aria-pressed',String(button.dataset.operation===this.planOperation));
    const rows=this.filteredPlan(),total=rows.length;
    this.planPage=Math.max(0,Math.min(this.planPage||0,Math.ceil(total/25)-1));
    const start=this.planPage*25,table=document.createElement('table'),head=table.createTHead().insertRow();
    for(const title of ['源文件','已有文件','媒体库目标','预期操作','说明']){const th=document.createElement('th');th.scope='col';th.textContent=title;head.append(th);}
    const body=table.createTBody();
    for(const i of rows.slice(start,start+25)) {
      const tr=body.insertRow();
      const values=[i.source||i.name,(i.existing_paths||[]).join('\n')||'—',i.target||i.planned_target||'—',operationNames[i.operation]||i.operation,i.explanation||i.reason||i.notice||'—'];
      values.forEach((v,n)=>{const td=tr.insertCell();td.textContent=v;if(n<3)td.className='path';});
    }
    this.$('plan').replaceChildren(table);
    this.$('plan-page').textContent=total?`${start+1}–${Math.min(start+25,total)} / ${total} 条（共 ${this.planRows.length} 条）`:'无匹配记录';
    this.syncPlanPagination();
  }
  watchProgress(ruleId) {
    clearTimeout(this.progressTimer);
    const poll=async()=>{
      if(!this.isConnected)return;
      try {
        const p=await this.host.api.get('progress'+(ruleId?'?rule_id='+ruleId:''));
        this.renderProgress(p);
      } catch {if(this.$('progress-current'))this.$('progress-current').textContent='暂时无法读取进度，正在重试…';}
      if(this.isConnected)this.progressTimer=setTimeout(poll,2000);
    };
    poll();
  }
  disconnectedCallback() {clearTimeout(this.progressTimer);this.focusCleanup?.();}
  renderProgress(p) {
    this.$('progress-panel').hidden=!p;if(!p)return;
    const labels={running:'正在整理',completed:'文件处理完成',interrupted:'整理已中断，请重新预览后继续'};
    const prefix=this.rid&&p.rule_id===null?'全局任务 · ':'';
    this.$('progress-label').textContent=prefix+(p.phase==='preview'?(p.state==='running'?'正在核验预览（只读）':'预览核验完成'):labels[p.state])+(p.state==='interrupted'?'':` · ${p.completed} / ${p.total} 个文件`);
    this.$('progress-bar').max=p.total||1;this.$('progress-bar').value=p.completed;
    this.$('progress-bar').hidden=p.state==='interrupted';
    this.$('progress-current').textContent=p.current?'当前：'+p.current:'';
    const names={reused:'复用副本',hardlink:'硬链',reflink:'Reflink',copy:'普通复制',existing:'已有文件',exists:'已存在',held:'待确认',failed:'失败',skipped:'已跳过'};
    this.$('progress-counts').textContent=Object.entries(p.counts).map(([k,n])=>`${names[k]||k} ${n}`).join(' · ');
  }
  message(text,error=false) {this.$('message').textContent=text;this.$('message').className=error?'error':'';}
  action(id,fn) {this.$(id).onclick=()=>this.run(fn);}
  async run(fn) {
    if(this.busy)return;
    this.busy=true;
    const controls=[...this.shadowRoot.querySelectorAll('button,input,select')];
    const disabled=controls.map(c=>c.disabled);
    controls.forEach(c=>c.disabled=true);
    try {await fn();} catch(e) {this.message(typeof e.message==='string'?e.message:JSON.stringify(e.message||e),true);}
    finally {
      controls.forEach((c,i)=>c.disabled=disabled[i]);
      this.busy=false;
      this.syncButtons?.();
    }
  }
}
class Settings extends Panel {
  connectedCallback() {
    this.mount(`<details open><summary>媒体库整理 · 全局设置 ${info('settings','目录、命名模板及版本后缀在插件选项中统一配置。优先硬链，再尝试 Reflink、普通复制；普通复制占用额外空间。音乐库留空时先归档，配置后可补整理。字体等附件保留原目录。单番纠错在番剧详情 → 媒体库。')}</summary><div class="body">
      <div class="row"><span id="state" class="badge"></span><button id="activate">接管整理</button>${info('takeover','接管前备份配置，并关闭原生重命名及硬链。下载文件保留原名；关闭自动整理不删除已有入库文件。')}</div>
      <div class="row"><span id="automatic-state" class="badge" role="status"></span><button id="automatic-action">预览并开启自动整理</button>${info('automatic','默认跟随 AB 标题、年份与季集偏移，无需逐番绑定。开启前先预览并确认全部计划；之后每分钟检查新下载和历史资源。手动删除的已整理文件不自动重建，可在单番详情手动补链。')}</div>
      <div class="row"><button id="status">刷新状态</button><button id="collections">更新作品合集</button>${info('collections','校验已成功整理的记录并更新动画、电影和专辑的关联清单，不移动或复制媒体文件。新下载整理成功后自动更新。清单目录单独扫描，容器路径不同时配置合集引用路径映射。无需媒体服务器 API 凭据。')}</div>
      <details><summary>音乐目录已在外部移动 ${info('moved','先关闭自动整理，用 DSM 等工具移动目录并调整容器挂载，在插件选项保存新音乐库路径。此处将新位置与原下载逐文件核验 SHA-256，再更新历史、归属和合集引用；不移动、重命名或复制媒体。原下载须仍可读取。任一文件校验失败则整批不更新，执行前自动备份。')}</summary><div class="body">
        <label>原音乐库路径（AB 内路径）<input id="moved-old" placeholder="/media/anime cds/albums"></label>
        <p>当前音乐库：<span id="moved-new"></span></p>
        <div class="row"><button id="preview-moved">预览路径记录更新</button><button id="confirm-moved" class="primary" hidden>仅更新路径记录</button><span id="moved-state" class="badge" role="status"></span></div>
        <div id="moved-plan" class="table-scroll" hidden></div>
      </div></details>
      ${planMarkup}<div class="row"><button id="confirm-auto" class="primary" hidden>执行以上计划并开启自动整理</button><button id="cancel-auto" hidden>取消</button></div>
      ${progressMarkup}<div id="message" role="status"></div>
      <div class="row"><span id="version"></span><button id="check">检查更新</button><button id="update" hidden></button>${info('update','每 6 小时自动检查新版，点击一次更新。更新前备份；启动失败恢复旧代码。')}</div>
    </div></details>`);
    this.action('activate',async()=>{
      if(!window.confirm('接管整理？将备份配置并关闭原生整理和硬链；本操作本身不会建立媒体链接。'))return;
      await this.host.api.post('activate',{});await this.status();
    });
    this.action('automatic-action',async()=>{
      const enable=!this.automatic;
      this.autoToken=null;
      if(enable) {
        if(!this.active)throw new Error('请先接管整理，再开启自动整理。');
        this.message('正在读取全部完成下载并核验已有文件，尚未开始整理…');
        const p=await this.host.api.post('automatic/preview',{});
        this.renderPlan(p);this.autoToken=p.token;
        this.$('confirm-auto').hidden=false;this.$('cancel-auto').hidden=false;
        this.message('预览已生成，确认后才执行并开启。');
      } else {await this.host.api.post('automatic',{enabled:false});await this.status();}
    });
    this.initPlan();
    this.action('cancel-auto',async()=>{
      this.autoToken=null;this.$('review').hidden=true;this.$('confirm-auto').hidden=true;this.$('cancel-auto').hidden=true;
      this.message('已取消，自动整理保持关闭。');
    });
    this.action('confirm-auto',async()=>{
      const token=this.autoToken;if(!token)return;this.autoToken=null;
      this.message('正在核对预览并执行；文件处理完成后开启自动整理…');
      try {
        const r=await this.host.api.post('automatic',{enabled:true,token});
        await this.status();
        this.message(summary(r.results)+(r.automatic?'\n自动整理已开启，新下载会继续按相同规则整理。':'\n存在执行失败，自动整理保持关闭；请重新预览。'));
      } finally {this.$('confirm-auto').hidden=true;this.$('cancel-auto').hidden=true;}

    });
    this.action('status',()=>this.status());
    this.action('collections',async()=>{
      const r=await this.host.api.post('collections/refresh',{});
      this.message((r.collections||[]).map(x=>x.detail).join('\n')||'暂无可生成的作品合集。');
    });
    this.$('moved-old').oninput=()=>{this.movedToken=null;this.$('confirm-moved').hidden=true;this.$('moved-state').textContent='';};
    this.action('preview-moved',async()=>{
      this.movedToken=null;this.$('confirm-moved').hidden=true;
      this.$('moved-state').textContent='正在逐文件校验';
      try {
        const p=await this.host.api.post('music-relocation/preview',{old_root:this.$('moved-old').value.trim()});
        const table=document.createElement('table'),head=table.createTHead().insertRow();
        for(const title of ['原记录路径','实际文件路径','校验结果']){const th=document.createElement('th');th.scope='col';th.textContent=title;head.append(th);}
        const body=table.createTBody();
        for(const item of p.items){const row=body.insertRow();[item.old_target,item.target,item.detail].forEach((text,index)=>{const cell=row.insertCell();cell.textContent=text;if(index<2)cell.className='path';if(item.state==='held')cell.classList.add('error');});}
        this.$('moved-plan').replaceChildren(table);this.$('moved-plan').hidden=false;
        const verified=p.items.filter(item=>item.state==='verified').length;
        this.$('moved-state').textContent=`校验一致 ${verified} / ${p.items.length}`;
        this.movedToken=p.ready?p.token:null;this.$('confirm-moved').hidden=!p.ready;
        this.message(p.ready?'校验通过，确认后仅更新记录和合集清单。':'存在未通过校验的文件；整批记录保持原样。',!p.ready);
      } catch(error){this.$('moved-state').textContent='校验未完成';throw error;}
    });
    this.action('confirm-moved',async()=>{
      const token=this.movedToken;if(!token)return;this.movedToken=null;this.$('confirm-moved').hidden=true;
      this.$('moved-state').textContent='正在复核并更新记录';
      try {
        const r=await this.host.api.post('music-relocation/apply',{token});
        await this.status();this.$('moved-state').textContent=`已更新 ${r.files} 个文件的记录`;
        this.message(`已更新 ${r.files} 个文件的路径记录，媒体文件未移动或复制。自动整理保持关闭，请重新预览后开启。\n`+(r.collections||[]).map(x=>x.detail).join('\n'));
      } catch(error){this.$('moved-state').textContent='更新未完成，请重新预览';throw error;}
    });
    this.action('check',async()=>this.updateStatus(await this.host.api.post('update/check',{})));
    this.action('update',async()=>{
      this.message('正在备份并更新…');
      await this.host.api.post('update/install',{version:this.latest});window.location.reload();
    });
    this.run(()=>this.status());this.watchProgress();
  }
  async status() {
    const s=await this.host.api.get('status');
    this.active=s.active;this.automatic=s.automatic;
    this.$('moved-new').textContent=s.music_root||'尚未配置';
    this.$('state').textContent=s.active?'已接管':'预览模式';
    this.$('automatic-state').textContent=s.automatic?'自动整理已开启':'自动整理已关闭';
    this.$('automatic-state').classList.toggle('enabled',s.automatic);
    const reports=(s.collections||[]).map(x=>x.detail).join('\n');
    this.message(s.error||[(s.results?.length?'上次整理：'+summary(s.results):'尚未执行整理。'),reports].filter(Boolean).join('\n'),Boolean(s.error));
    this.updateStatus(s.update);
  }
  updateStatus(u) {
    this.$('version').textContent=`v${u.current}${u.error?' · '+u.error:''}`;
    this.latest=u.available?u.version:null;
    this.$('update').hidden=!this.latest;this.$('update').textContent=`更新到 ${u.version}`;
  }
  syncButtons() {
    this.$('update').disabled=!this.latest;this.$('confirm-auto').disabled=!this.autoToken;
    this.$('automatic-action').textContent=this.automatic?'关闭自动整理':this.autoToken?'重新预览整理计划':'预览并开启自动整理';
    this.$('automatic-action').disabled=!this.active&&!this.automatic;
    this.$('preview-moved').disabled=!this.active||this.automatic;
    this.$('confirm-moved').disabled=!this.movedToken||this.automatic;
    if(this.$('review'))this.syncPlanPagination();
  }
}
class Detail extends Panel {
  connectedCallback() {
    // AB 4.0's Vueuc focus trap uses the deepest composed target with Node.contains(),
    // which cannot cross Shadow DOM. Retarget only this panel's focus events to
    // AB's light-DOM slot, matching the native event.target seen by the document.
    // Do not disable the modal trap or alter focus events outside this plugin.
    const lightHost=this.getRootNode().host;
    const dialog=lightHost?.closest('[role="dialog"]');
    if(dialog) {
      const retarget=event=>{
        const path=event.composedPath();
        if(path.includes(this)&&path.includes(lightHost)&&dialog.contains(lightHost)) {
          const visible=path.slice(path.indexOf(lightHost));
          Object.defineProperty(event,'composedPath',{value:()=>visible});
        }
      };
      window.addEventListener('focus',retarget,true);
      this.focusCleanup=()=>window.removeEventListener('focus',retarget,true);
    }
    this.rid=Number(this.context?.bangumiId);
    this.mount(`<p id="state" class="muted"></p>
      <p>默认跟随 AB，无需另行配置，下载完成后自动入库。需要纠错时再覆盖 TMDB；设置对后续下载持续生效。</p>
      <label>元数据来源<select id="metadata-source"><option value="ab">跟随 AB（默认）</option><option value="tmdb">手动指定 TMDB</option></select></label>
      <div id="tmdb-fields" hidden>
      <div class="row"><label>类型<select id="type"><option value="tv">剧集</option><option value="movie">电影</option></select></label><label>搜索名称<input id="query"></label><button id="search">搜索 TMDB</button></div>
      <label>TMDB 条目<select id="matches"><option value="">请搜索或填写 TMDB ID</option></select></label>
      <label>TMDB ID<input id="tmdb" type="number" min="1"></label></div><p id="ab-mapping" class="muted"></p>
      <label>整理范围<select id="scope"><option value="all">全部资源</option><option value="resources">仅音乐与归档附件</option></select></label><p class="muted">仅音乐与归档附件：保留已有视频、字幕和外挂音轨的位置；后续自动整理也遵循此范围。</p>
      <div class="row"><button id="save" class="primary">保存本番设置</button><button id="scan">刷新下载任务</button></div>
      <p id="task-count" class="muted">点击“刷新下载任务”查看已有资源；设置可在下载完成前保存。</p>
      <details id="task-panel"><summary>下载任务与逐文件设置</summary><div class="body">
        <label>本番下载任务<select id="task"></select></label><p id="association" class="muted"></p><p id="inherit-state" class="muted"></p>
        <div id="override-fields">
          <div class="row"><label>此任务的季<input id="task-season" type="number" min="0"></label><label>此任务集数偏移<input id="task-offset" type="number"></label></div>
          <p class="muted">逐文件填写集号可覆盖自动识别；特别篇选择 Season 00 并填写目标集号（手动指定 TMDB 时须与其特别篇对应）。留空时特典按附加视频整理，不猜编号。这里的设置只影响当前下载任务。</p>
          <div id="episodes" class="plan"></div>
          <label>电影正片<select id="main"></select></label>
          <div class="row"><button id="save-task">保存此任务独立设置</button><button id="inherit">恢复跟随本番设置</button></div>
        </div>
      </div></details>
      <details id="unassigned-panel" hidden><summary>关联尚未归属的下载任务</summary><div class="body">
        <p class="muted">仅在确定任务属于本番时关联；不会改动下载器标签或文件。</p>
        <select id="unassigned"></select><div class="row"><button id="assign">关联到本番</button></div>
      </div></details>
      <div class="row"><button id="preview">预览本番整理计划</button><button id="apply" class="primary" disabled>执行此计划</button></div>
      ${planMarkup}<details id="result-panel" hidden><summary>上次整理明细</summary><div id="results" class="table-scroll"></div><div class="row"><button id="result-prev">上一页</button><span id="result-page"></span><button id="result-next">下一页</button></div></details>${progressMarkup}<div id="message" role="status"></div>`);
    for(const id of ['type','query','tmdb','scope'])this.$(id).oninput=()=>{this.dirty=true;this.invalidate();};
    this.action('search',async()=>{
      const rows=await this.host.api.get(`search?query=${encodeURIComponent(this.$('query').value)}&kind=${this.$('type').value}`);
      this.$('matches').replaceChildren(new Option('请选择',''),...rows.map(r=>new Option(`${r.title} (${r.year}) [${r.id}]`,r.id)));
    });
    this.$('metadata-source').onchange=()=>{this.$('tmdb-fields').hidden=this.$('metadata-source').value!=='tmdb';this.dirty=true;this.invalidate();};
    this.$('matches').onchange=()=>{this.$('tmdb').value=this.$('matches').value;this.dirty=true;this.invalidate();};
    this.action('save',async()=>{
      await this.host.api.post(`rules/${this.rid}/binding`,this.mappingForm());
      this.dirty=false;this.invalidate();await this.loadRule();if(this.tasks)await this.scan();this.message('本番设置已保存，后续下载将使用此映射。');
    });
    this.action('scan',()=>this.scan());
    this.$('task').onchange=()=>this.choose();
    this.action('save-task',async()=>{
      const task=this.selected();const base=task?.override||this.effectiveMapping;
      if(!task||!base)throw new Error('请先刷新本番下载任务。');
      const mapping={...base,season:Number(this.$('task-season').value),episode_offset:Number(this.$('task-offset').value),
        episodes:this.fileMapping(false),specials:this.fileMapping(true),main_file:this.$('main').value||null};
      await this.host.api.post(`rules/${this.rid}/tasks/${task.key}/binding`,mapping);
      this.invalidate();await this.scan();this.message('当前任务的独立设置已保存；其他下载继续跟随本番设置。');
    });
    this.action('inherit',async()=>{
      const t=this.selected();this.syncButtons();if(!t)return;
      await this.host.api.post(`rules/${this.rid}/tasks/${t.key}/inherit`,{});
      this.invalidate();await this.scan();this.message('此任务已恢复跟随本番设置。');
    });
    this.action('assign',async()=>{
      const key=this.$('unassigned').value;if(!key)return;
      await this.host.api.post(`rules/${this.rid}/assign`,{task_key:key});
      this.invalidate();await this.scan();this.message('任务已关联到本番。');
    });
    this.action('preview',async()=>{
      this.invalidate();if(this.dirty)throw new Error('请先保存本番设置，再预览。');
      const p=await this.host.api.post(`rules/${this.rid}/preview`,{});
      this.token=p.linkable?p.token:null;
      this.renderPlan(p);
      this.message(`仅当前番：${p.linkable} 个可整理，${p.held} 个待确认。`);
    });
    this.action('apply',async()=>{
      const token=this.token;this.invalidate();this.message('正在核对下载和配置，随后开始整理…');
      const r=await this.host.api.post(`rules/${this.rid}/apply`,{token});
      this.renderResults(r.results);this.message(summary(r.results)+'\n'+(r.collections||[]).map(x=>x.detail).join('\n')+'\n'+r.results.filter(x=>x.state==='failed').map(x=>x.detail).join('\n'));
    });
    this.initPlan();
    this.action('result-prev',()=>{this.resultPage--;this.renderResultPage();});
    this.action('result-next',()=>{this.resultPage++;this.renderResultPage();});
    this.run(async()=>{if(!Number.isInteger(this.rid)||this.rid<1)throw new Error('无法确定当前番剧');await this.loadRule();});this.watchProgress(this.rid);
  }
  fileMapping(special) {return Object.fromEntries([...this.$('episodes').querySelectorAll('input')].filter(i=>i.value!==''&&(i.parentElement.querySelector('select').value==='special')===special).map(i=>[i.dataset.file,Number(i.value)]));}
  mappingForm() {
    const tmdb=this.$('metadata-source').value==='tmdb';
    const id=Number(this.$('tmdb').value);
    if(tmdb&&(!Number.isInteger(id)||id<1))throw new Error('请搜索或填写有效的 TMDB ID。');
    return {scope:this.$('scope').value,media_type:tmdb?this.$('type').value:this.abRule.media_type,tmdb_id:tmdb?id:null,season:this.abRule?.season??1,episode_offset:this.abRule?.episode_offset??0,episodes:{},main_file:null};
  }
  invalidate() {this.token=null;if(this.$('apply'))this.$('apply').disabled=true;if(this.$('review'))this.$('review').hidden=true;}
  async loadRule() {
    const data=await this.host.api.get(`rules/${this.rid}/library`);this.mapping=data.mapping;
    this.renderResults(data.results||[]);
    const m=data.effective_mapping;this.effectiveMapping=m;
    this.$('metadata-source').value=data.metadata_source;this.$('tmdb-fields').hidden=data.metadata_source!=='tmdb';
    this.$('scope').value=m.scope||'all';this.$('type').value=m.media_type||'tv';this.$('query').value=data.rule.title;
    this.$('tmdb').value=m.tmdb_id||'';this.abRule=data.rule;this.$('ab-mapping').textContent=`AB：${data.rule.title}${data.rule.year?` (${data.rule.year})`:""} · ${data.rule.media_type==="movie"?"电影":"剧集"} · 第 ${data.rule.season} 季，集数偏移 ${data.rule.episode_offset}。季集跟随 AB「规则」，修改后下次整理生效。`;
    this.$('state').textContent=!data.active?'预览模式 · 请到全局设置接管整理':data.automatic?'自动整理已开启 · 下载完成后自动入库':'自动整理已关闭 · 可以手动整理本番';
  }
  renderResults(rows) {
    this.resultRows=rows;this.resultPage=0;
    this.$('result-panel').hidden=!rows.length;
    this.renderResultPage();
  }
  syncPagination() {
    if(!this.$('result-prev'))return;
    this.$('result-prev').disabled=!this.resultPage;
    this.$('result-next').disabled=(this.resultPage+1)*25>=(this.resultRows?.length||0);
  }
  renderResultPage() {
    const rows=this.resultRows||[],total=rows.length;
    this.resultPage=Math.max(0,Math.min(this.resultPage||0,Math.ceil(total/25)-1));
    const start=this.resultPage*25;
    const table=document.createElement('table'),head=table.createTHead().insertRow();
    for(const title of ['源文件','媒体库目标','处理方式','状态','说明']){const th=document.createElement('th');th.scope='col';th.textContent=title;head.append(th);}
    const body=table.createTBody(),methods={hardlink:'硬链',reflink:'Reflink',copy:'普通复制',existing:'已有文件（方式未知）'};
    for(const r of rows.slice(start,start+25)) {
      const tr=body.insertRow(),method=methods[r.method]||'—';
      const action=r.action==='relocated'?'复用副本 · '+method:r.action==='unchanged'?'已存在 · '+method:method;
      const values=[r.source||r.name||r.key,r.target||'—',action,stateNames[r.state]||r.state,r.detail||'—'];
      values.forEach((value,i)=>{const td=tr.insertCell();td.textContent=value;if(i<2)td.className='path';});
    }
    this.$('results').replaceChildren(table);
    this.$('result-page').textContent=total?`${start+1}–${Math.min(start+25,total)} / ${total} 条`:'暂无记录';
    this.syncPagination();
  }
  async scan() {
    const selected=this.$('task').value;
    const data=await this.host.api.post(`rules/${this.rid}/scan`,{});this.tasks=data.tasks;
    this.$('task').replaceChildren(...this.tasks.map(t=>new Option(t.name,t.key)));
    if(this.tasks.some(t=>t.key===selected))this.$('task').value=selected;
    this.$('task-count').textContent=this.tasks.length?`${this.tasks.length} 个已完成下载任务`:'尚无已关联的完成下载；后续下载完成后会自动跟随 AB 整理。';
    this.$('override-fields').hidden=!this.tasks.length;
    this.$('unassigned-panel').hidden=!data.unassigned.length;
    this.$('unassigned').replaceChildren(...data.unassigned.map(t=>{
      const option=new Option(`${t.name} · ${t.association?.reason||'待确认归属'}`,t.key);
      option.disabled=t.association?.assignable===false;return option;
    }));
    this.choose();
  }
  selected() {return this.tasks?.find(t=>t.key===this.$('task').value);}
  choose() {
    const t=this.selected();this.syncButtons();if(!t)return;
    const a=t.association||{};
    this.$('association').textContent=`归属：entry ${t.rule_id} · ${a.reason||'已关联'}${a.record_ids?.length?' · AB 种子记录 '+a.record_ids.join(', '):''}`;
    const m=t.override||this.effectiveMapping||{};
    this.$('inherit-state').textContent=t.override?`此任务使用独立设置（${m.tmdb_id?"TMDB "+m.tmdb_id:"元数据跟随 AB"}）`:'此任务跟随本番设置，修改本番映射后会一同更新。';
    this.$('task-season').value=m.season??1;this.$('task-offset').value=m.episode_offset??0;
    const videos=t.files.filter(f=>/\.(mkv|mp4|avi|m4v|ts|webm|mov)$/i.test(f.name));
    this.$('episodes').replaceChildren(...videos.map(f=>{
      const label=document.createElement('label');label.textContent=f.name;
      const input=document.createElement('input');input.type='number';input.min='0';input.placeholder='自动识别';input.dataset.file=f.name;input.value=m.specials?.[f.name]??m.episodes?.[f.name]??'';
      const season=document.createElement('select');season.append(new Option('跟随此任务的季','regular'),new Option('特别篇 · Season 00','special'));season.value=m.specials?.[f.name]!==undefined?'special':'regular';
      label.append(season,input);return label;
    }));
    this.$('main').replaceChildren(new Option(m.tmdb_id?'自动（仅单一候选时）':'自动（选取体积最大的候选正片）',''),...videos.map(f=>new Option(f.name,f.name)));
    this.$('main').value=m.main_file||'';
  }
  syncButtons() {this.syncPagination();this.syncPlanPagination();this.$('apply').disabled=!this.token;this.$('save-task').disabled=!this.selected();this.$('inherit').disabled=!this.selected()?.override;}
}
if(!customElements.get('ab-plugin-library-organizer'))customElements.define('ab-plugin-library-organizer',Settings);
if(!customElements.get('ab-plugin-library-organizer-detail'))customElements.define('ab-plugin-library-organizer-detail',Detail);
