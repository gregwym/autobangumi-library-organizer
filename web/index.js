const css=`:host{display:block;font:14px/1.5 var(--ab-font-family,system-ui);color:var(--ab-color-text,#222)}*{box-sizing:border-box}details{border:1px solid var(--ab-color-border,#ddd);border-radius:var(--ab-radius-md,8px);overflow:hidden}summary{padding:7px 14px;font-weight:600;cursor:pointer;color:var(--ab-color-text-secondary,#666)}.body{padding:14px;background:var(--ab-color-surface,#fff);border-top:1px solid var(--ab-color-border,#ddd)}.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:10px 0}input,select,button,textarea{font:inherit;color:inherit;background:transparent;border:1px solid var(--ab-color-border,#aaa);border-radius:6px;padding:7px 10px}button{cursor:pointer}button:disabled{opacity:.5;cursor:default}.primary{background:var(--ab-color-primary,#5274d9);color:white}label{display:grid;gap:5px;margin:8px 0}label:has(input[type=checkbox]){display:flex;align-items:center;gap:8px}input[type=checkbox]{width:auto;margin:0;accent-color:var(--ab-color-primary,#526fe4)}select,textarea{max-width:100%;width:100%}textarea{min-height:70px}small,.muted{color:var(--ab-color-text-secondary,#777)}.plan{max-height:400px;overflow:auto}.item{border-bottom:1px solid var(--ab-color-border,#ddd);padding:9px 0;overflow-wrap:anywhere}#message{white-space:pre-wrap;margin-top:10px}.badge{border-radius:999px;padding:2px 9px;background:#8882}.error{color:#c65540}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;vertical-align:top;padding:9px 10px;border-bottom:1px solid var(--ab-color-border,#ddd)}th{position:sticky;top:0;background:var(--ab-color-surface,#fff)}td.path{min-width:230px;max-width:340px;overflow-wrap:anywhere;white-space:pre-wrap}td{min-width:90px}.table-scroll{overflow:auto;max-height:400px}[hidden]{display:none!important}`;
const progressMarkup = `<section id="progress-panel" hidden aria-live="polite"><p id="progress-label"></p><progress aria-label="文件整理进度" id="progress-bar" max="1" value="0" style="width:100%;accent-color:var(--ab-color-primary,#5274d9)"></progress><p id="progress-current" class="muted" style="overflow-wrap:anywhere"></p><small id="progress-counts"></small></section>`;
const stateNames = {linked:'已整理',exists:'已有链接',held:'待确认',failed:'失败'};
function summary(rows) {
  const counts={}; for(const r of rows){const key=r.action==='relocated'?'复用副本':r.state==='linked'&&r.method?({hardlink:'硬链',reflink:'Reflink',copy:'普通复制'}[r.method]||r.state):r.state;counts[key]=(counts[key]||0)+1;}
  return Object.entries(counts).map(([key,n])=>`${stateNames[key]||key} ${n}`).join(' · ');
}
class Panel extends HTMLElement {
  mount(body) {
    if(!this.shadowRoot)this.attachShadow({mode:'open'});
    this.shadowRoot.innerHTML=`<style>${css}</style>${body}`;
    this.$=id=>this.shadowRoot.getElementById(id);
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
    this.$('progress-label').textContent=prefix+labels[p.state]+(p.state==='interrupted'?'':` · ${p.completed} / ${p.total} 个文件`);
    this.$('progress-bar').max=p.total||1;this.$('progress-bar').value=p.completed;
    this.$('progress-bar').hidden=p.state==='interrupted';
    this.$('progress-current').textContent=p.current?'当前：'+p.current:'';
    const names={reused:'复用副本',hardlink:'硬链',reflink:'Reflink',copy:'普通复制',exists:'已存在',held:'待确认',failed:'失败'};
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
    this.mount(`<details open><summary>媒体库整理 · 全局设置</summary><div class="body">
      <p>下载根目录、视频库、可选音乐库、版本后缀和合集清单目录在插件配置中统一设置。优先硬链，不支持时依次尝试 Reflink、普通复制（占用额外空间）。</p>
      <p class="muted">音乐库留空时先归档；配置后自动补整理已归档音乐。专辑与合集清单均须纳入媒体服务器扫描，两个容器内的音乐路径须相同。插件不持有媒体服务器 API 凭据。字体、扫描图和其他附件保留原目录归档。</p><p class="muted">每部番的 TMDB 绑定、季集映射和整理预览，请到「番剧详情 → 媒体库」。</p>
      <div class="row"><span id="state" class="badge"></span><button id="activate">接管整理</button></div>
      <label><input id="automatic" type="checkbox">自动整理已绑定的番剧</label>
      <small>开启后每分钟检查完成的下载，包括已绑定但尚未整理的旧下载。关闭后仍可在番剧详情手动预览、整理。</small>
      <p class="muted">接管会备份配置并关闭原生整理与硬链。下载文件保留原名，停用自动整理不会删除已建链接。</p>
      <div class="row"><button id="status">刷新状态</button></div>
      ${progressMarkup}<div id="message" role="status"></div>
      <div class="row"><span id="version"></span><button id="check">检查更新</button><button id="update" hidden></button></div>
      <small>每 6 小时检查新版，点击一次更新；更新前备份，启动失败恢复旧代码。</small>
    </div></details>`);
    this.action('activate',async()=>{
      if(!window.confirm('接管整理？将备份配置并关闭原生整理和硬链；本操作本身不会建立媒体链接。'))return;
      await this.host.api.post('activate',{});await this.status();
    });
    this.$('automatic').onchange=()=>this.run(async()=>{
      try {await this.host.api.post('automatic',{enabled:this.$('automatic').checked});}
      finally {await this.status();}
    });
    this.action('status',()=>this.status());
    this.action('check',async()=>this.updateStatus(await this.host.api.post('update/check',{})));
    this.action('update',async()=>{
      this.message('正在备份并更新…');
      await this.host.api.post('update/install',{version:this.latest});window.location.reload();
    });
    this.run(()=>this.status());this.watchProgress();
  }
  async status() {
    const s=await this.host.api.get('status');
    this.$('state').textContent=s.active?'已接管':'预览模式';this.$('automatic').checked=s.automatic;
    const reports=(s.collections||[]).map(x=>x.detail).join('\n');
    this.message(s.error||[(s.results?.length?'上次整理：'+summary(s.results):'尚未执行整理。'),reports].filter(Boolean).join('\n'),Boolean(s.error));
    this.updateStatus(s.update);
  }
  updateStatus(u) {
    this.$('version').textContent=`v${u.current}${u.error?' · '+u.error:''}`;
    this.latest=u.available?u.version:null;
    this.$('update').hidden=!this.latest;this.$('update').textContent=`更新到 ${u.version}`;
  }
  syncButtons() {this.$('update').disabled=!this.latest;}
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
      <p>本番的设置对后续下载持续生效。自动整理开启时，保存后会在下次扫描生效。</p>
      <div class="row"><label>类型<select id="type"><option value="tv">剧集</option><option value="movie">电影</option></select></label><label>搜索名称<input id="query"></label><button id="search">搜索 TMDB</button></div>
      <label>TMDB 条目<select id="matches"><option value="">请搜索或填写 TMDB ID</option></select></label>
      <label>TMDB ID<input id="tmdb" type="number" min="1"></label><p id="ab-mapping" class="muted"></p>
      <label>整理范围<select id="scope"><option value="all">全部资源</option><option value="resources">仅音乐与归档附件</option></select></label><p class="muted">仅音乐与归档附件：保留已有视频、字幕和外挂音轨的位置；后续自动整理也遵循此范围。</p>
      <div class="row"><button id="save" class="primary">保存本番设置</button><button id="scan">刷新下载任务</button></div>
      <p id="task-count" class="muted">点击“刷新下载任务”查看已有资源；设置可在下载完成前保存。</p>
      <details id="task-panel"><summary>下载任务与逐文件设置</summary><div class="body">
        <label>本番下载任务<select id="task"></select></label><p id="association" class="muted"></p><p id="inherit-state" class="muted"></p>
        <div id="override-fields">
          <div class="row"><label>此任务的季<input id="task-season" type="number" min="0"></label><label>此任务集数偏移<input id="task-offset" type="number"></label></div>
          <p class="muted">逐文件填写集号可覆盖自动识别；特别篇选择 Season 00 并填写 TMDB 特别篇集号。留空时特典按附加视频整理，不猜编号。这里的设置只影响当前下载任务。</p>
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
      <div id="plan" class="plan"></div><details id="result-panel" hidden><summary>上次整理明细</summary><div id="results" class="table-scroll"></div><div class="row"><button id="result-prev">上一页</button><span id="result-page"></span><button id="result-next">下一页</button></div></details>${progressMarkup}<div id="message" role="status"></div>`);
    for(const id of ['type','query','tmdb','scope'])this.$(id).oninput=()=>{this.dirty=true;this.invalidate();};
    this.action('search',async()=>{
      const rows=await this.host.api.get(`search?query=${encodeURIComponent(this.$('query').value)}&kind=${this.$('type').value}`);
      this.$('matches').replaceChildren(new Option('请选择',''),...rows.map(r=>new Option(`${r.title} (${r.year}) [${r.id}]`,r.id)));
    });
    this.$('matches').onchange=()=>{this.$('tmdb').value=this.$('matches').value;this.dirty=true;this.invalidate();};
    this.action('save',async()=>{
      await this.host.api.post(`rules/${this.rid}/binding`,this.mappingForm());
      this.dirty=false;this.invalidate();await this.loadRule();if(this.tasks)await this.scan();this.message('本番设置已保存，后续下载将使用此映射。');
    });
    this.action('scan',()=>this.scan());
    this.$('task').onchange=()=>this.choose();
    this.action('save-task',async()=>{
      const task=this.selected();const base=task?.override||this.mapping;
      if(!task||!base)throw new Error('请先保存本番 TMDB 设置，再配置具体任务。');
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
      this.$('plan').replaceChildren(...p.items.map(i=>{
        const el=document.createElement('div');el.className='item';el.style.whiteSpace='pre-wrap';
        el.textContent=`${i.name}\n→ ${i.target||i.reason}${i.notice?'\n'+i.notice:''}${i.old_targets?.length?'\n旧路径：'+i.old_targets.join(', ')+'（验证新文件后，仅清理本插件管理的文件）':''}`;return el;
      }));
      this.message(`仅当前番：${p.linkable} 个可整理，${p.held} 个待确认。`);
    });
    this.action('apply',async()=>{
      const token=this.token;this.invalidate();this.message('正在核对下载和配置，随后开始整理…');
      const r=await this.host.api.post(`rules/${this.rid}/apply`,{token});
      this.renderResults(r.results);this.message(summary(r.results)+'\n'+(r.collections||[]).map(x=>x.detail).join('\n')+'\n'+r.results.filter(x=>x.state==='failed').map(x=>x.detail).join('\n'));
    });
    this.action('result-prev',()=>{this.resultPage--;this.renderResultPage();});
    this.action('result-next',()=>{this.resultPage++;this.renderResultPage();});
    this.run(async()=>{if(!Number.isInteger(this.rid)||this.rid<1)throw new Error('无法确定当前番剧');await this.loadRule();});this.watchProgress(this.rid);
  }
  fileMapping(special) {return Object.fromEntries([...this.$('episodes').querySelectorAll('input')].filter(i=>i.value!==''&&(i.parentElement.querySelector('select').value==='special')===special).map(i=>[i.dataset.file,Number(i.value)]));}
  mappingForm() {return {scope:this.$('scope').value,media_type:this.$('type').value,tmdb_id:Number(this.$('tmdb').value),season:this.abRule?.season??1,episode_offset:this.abRule?.episode_offset??0,episodes:{},main_file:null};}
  invalidate() {this.token=null;if(this.$('apply'))this.$('apply').disabled=true;}
  async loadRule() {
    const data=await this.host.api.get(`rules/${this.rid}/library`);this.mapping=data.mapping;
    this.renderResults(data.results||[]);
    const m=data.mapping||data.rule;
    this.$('scope').value=m.scope||'all';this.$('type').value=m.media_type||'tv';this.$('query').value=data.rule.title;
    this.$('tmdb').value=m.tmdb_id||'';this.abRule=data.rule;this.$('ab-mapping').textContent=`季集跟随 AB「规则」：第 ${data.rule.season} 季，集数偏移 ${data.rule.episode_offset}。修改原有规则后，下次整理自动使用新值。`;
    this.$('state').textContent=!data.active?'预览模式 · 请到全局设置接管整理':data.automatic?'自动整理已开启 · 下载完成后会使用本番映射':'自动整理已关闭 · 可以手动整理本番';
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
    const body=table.createTBody(),methods={hardlink:'硬链',reflink:'Reflink',copy:'普通复制'};
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
    this.$('task-count').textContent=this.tasks.length?`${this.tasks.length} 个已完成下载任务`:'尚无已关联的完成下载；仍可先保存本番映射，等待后续下载。';
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
    const m=t.override||this.mapping||{};
    this.$('inherit-state').textContent=t.override?`此任务使用独立设置（TMDB ${m.tmdb_id}）`:'此任务跟随本番设置，修改本番映射后会一同更新。';
    this.$('task-season').value=m.season??1;this.$('task-offset').value=m.episode_offset??0;
    const videos=t.files.filter(f=>/\.(mkv|mp4|avi|m4v|ts|webm|mov)$/i.test(f.name));
    this.$('episodes').replaceChildren(...videos.map(f=>{
      const label=document.createElement('label');label.textContent=f.name;
      const input=document.createElement('input');input.type='number';input.min='0';input.placeholder='自动识别';input.dataset.file=f.name;input.value=m.specials?.[f.name]??m.episodes?.[f.name]??'';
      const season=document.createElement('select');season.append(new Option('跟随此任务的季','regular'),new Option('特别篇 · Season 00','special'));season.value=m.specials?.[f.name]!==undefined?'special':'regular';
      label.append(season,input);return label;
    }));
    this.$('main').replaceChildren(new Option('自动（仅单一候选时）',''),...videos.map(f=>new Option(f.name,f.name)));
    this.$('main').value=m.main_file||'';
  }
  syncButtons() {this.syncPagination();this.$('apply').disabled=!this.token;this.$('save-task').disabled=!this.selected();this.$('inherit').disabled=!this.selected()?.override;}
}
if(!customElements.get('ab-plugin-library-organizer'))customElements.define('ab-plugin-library-organizer',Settings);
if(!customElements.get('ab-plugin-library-organizer-detail'))customElements.define('ab-plugin-library-organizer-detail',Detail);
