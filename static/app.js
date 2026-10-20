"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const state = {snapshot:null,history:null,minutes:60,chartMode:"live",livePoints:[],mode:"live",sort:{live:{key:"cpu_pct",dir:-1},history:{key:"cpu_avg",dir:-1}},search:"",series:new Set(["cpu_pct","gpu_pct","ram_pct"]),selectedGroup:null,historyRequest:0,chartPoints:[],lastSuccess:0};
  let snapshotDeadline=performance.now();
  const colors = {cpu_pct:"#82a8ff",gpu_pct:"#49dac3",ram_pct:"#bc9aff",vram_pct:"#edc783"};
  const seriesNames = {cpu_pct:"CPU",gpu_pct:"GPU",ram_pct:"메모리",vram_pct:"GPU 메모리"};
  const finite = (v) => typeof v === "number" && Number.isFinite(v);
  const number = (v,digits=1) => finite(v) ? v.toLocaleString("ko-KR",{minimumFractionDigits:digits,maximumFractionDigits:digits}) : "—";
  const percent = (v) => finite(v) ? `${number(v)}%` : "—";
  const gb = (v) => finite(v) ? `${number(v)} GiB` : "—";
  const mbps = (v) => finite(v) ? `${number(v,2)} MiB/s` : "—";
  const clamp = (v) => finite(v) ? Math.max(0,Math.min(100,v)) : 0;
  const safe = (v) => String(v ?? "").replace(/[&<>"']/g,(c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const time = (ts,seconds=false) => finite(ts) ? new Date(ts*1000).toLocaleTimeString("ko-KR",{hour:"2-digit",minute:"2-digit",...(seconds?{second:"2-digit"}:{}),hour12:false}) : "—";
  const date = (ts) => finite(ts) ? new Date(ts*1000).toLocaleString("ko-KR",{month:"long",day:"numeric",hour:"2-digit",minute:"2-digit",hour12:false}) : "—";
  const sum = (a,b) => finite(a)&&finite(b) ? a+b : null;
  const rows = (value) => Array.isArray(value) ? value : [];
  const elapsed = (minutes) => !finite(minutes) ? "미측정" : minutes<1 ? "1분 미만" : minutes<60 ? `${Math.floor(minutes)}분` : `${Math.floor(minutes/60)}시간 ${Math.floor(minutes%60)}분`;
  const memory = (v) => finite(v) ? (v<1 ? `${number(v*1024,0)} MiB` : gb(v)) : "—";
  const peakGpu = (gpus) => rows(gpus).reduce((best,g) => !best || (finite(g.util_pct)?g.util_pct:-1)>(finite(best.util_pct)?best.util_pct:-1) ? g : best,null);
  const engineName = (value) => {const match=String(value||"").match(/engtype_([a-z0-9_ -]+)/i);return match?match[1].replace(/_/g," "):value?"GPU 엔진":"";};

  const levelFor = (value,watch,high) => !finite(value)?"unknown":value>=high?"high":value>=watch?"watch":"safe";
  const levelNames = {safe:"여유",watch:"주의",high:"높음",unknown:"미측정"};
  // Display guidance, not manufacturer limits or automatic workload control.
  const thermalLimits = {cpu:[80,85],gpu:[80,90],ram:[65,80],disk:[55,65]};
  function setLevel(el,value,limits,description){
    const level=levelFor(value,...limits);
    el.dataset.level=level;
    el.title=`${description} · ${levelNames[level]} · 노랑 ${limits[0]} 이상 / 빨강 ${limits[1]} 이상`;
    return level;
  }
  function renderUsage(sys={},gpu=null,stale=false){
    const usage={cpu:sys.cpu_pct,gpu:gpu?.util_pct,ram:sys.ram_pct,disk:sys.disk_busy_pct};
    Object.entries(usage).forEach(([key,value])=>{
      const el=$(`${key}-value`),measured=stale?null:value;
      const limits=key==="ram"?[80,90]:[70,90];
      const description=key==="disk"?`가장 바쁜 물리 디스크 활동 ${percent(measured)} · 전송량과 별도`: `사용률 ${percent(measured)}`;
      const level=setLevel(el,measured,limits,description);
      el.setAttribute("aria-label",`${description} · ${levelNames[level]}${stale?" · 갱신 지연":""}`);
    });
  }
  function renderHardware(sys={},gpu=null){
    const inventory=sys.hardware||{},modules=rows(inventory.memory),disks=rows(inventory.storage),systemDisk=inventory.system_disk;
    const cpuName=(sys.cpu_name||"CPU 정보 확인 중").replace(/\s+\d+-Core Processor/i,"").trim();
    $("cpu-model").textContent=cpuName;$("cpu-model").title=sys.cpu_name||"";
    $("cpu-spec").textContent=[finite(sys.cpu_cores)?`${sys.cpu_cores}코어`:null,finite(sys.cpu_threads)?`${sys.cpu_threads}스레드`:null].filter(Boolean).join(" · ");
    $("gpu-model").textContent=gpu?.name?.replace(/^NVIDIA\s+/,"")||"GPU 정보 미측정";$("gpu-model").title=rows(sys.gpus).map(g=>g.name).filter(Boolean).join("\n");
    $("gpu-spec").textContent=finite(gpu?.vram_total_gb)?`전용 메모리 ${memory(gpu.vram_total_gb)}`:"전용 메모리 미측정";
    const types={24:"DDR3",26:"DDR4",34:"DDR5",30:"LPDDR4",35:"LPDDR5"},memoryTypes=[...new Set(modules.map(m=>types[m.type]).filter(Boolean))];
    const installed=modules.length?modules.reduce((total,m)=>total+(finite(m.capacity)?m.capacity:0),0)/1024**3:null;
    $("ram-model").textContent=[memoryTypes.join(" / "),finite(installed)&&installed>0?`${number(installed,0)} GB`:finite(sys.ram_total_gb)?`${number(sys.ram_total_gb)} GiB`:"정보 확인 중"].filter(Boolean).join(" · ");
    $("ram-model").title=modules.map(m=>`${m.part||"메모리 모듈"} · ${number(m.capacity/1024**3,0)} GB`).join("\n");
    $("ram-spec").textContent=modules.length?`${modules.length}개 모듈 · 사용 가능 ${number(sys.ram_total_gb)} GiB`:"시스템 메모리";
    $("disk-model").textContent=systemDisk?.name||"디스크 정보 확인 중";
    $("disk-model").title=disks.map(d=>`${d.name} · ${d.bus} · ${d.media} · ${number(d.size/1e12,2)} TB`).join("\n");
    $("disk-spec").textContent=systemDisk?[systemDisk.drive,systemDisk.bus,`${number(systemDisk.size/1e12,1)} TB`,disks.length>1?`외 ${disks.length-1}개`:null].filter(Boolean).join(" · "):"전체 물리 디스크";
    $("hardware-summary").textContent="장치별 사용량과 온도를 실시간으로 확인합니다.";
  }
  function renderNetwork(network={},stale=false){
    const kinds={wired:"유선",wireless:"무선 · Wi-Fi",virtual:"가상 · VPN",other:"기타 연결",disconnected:"연결 없음",unknown:"연결 확인 중"};
    const valid=!stale&&network.status==="ok";
    $("network-kind").textContent=stale?"갱신 지연":kinds[network.kind]||"연결 확인 중";
    $("network-kind").dataset.level=stale?"unknown":network.kind==="disconnected"?"high":network.status==="ok"?"safe":"unknown";
    $("network-model").textContent=network.name||"사용 중인 어댑터 확인 중";
    $("network-link").textContent=finite(network.link_mbps)&&network.link_mbps>0?`연결 속도 ${number(network.link_mbps,0)} Mbps`:"연결 속도 미측정";
    $("network-link").title="PC와 공유기·스위치 사이의 연결 속도입니다. 인터넷 최대 속도가 아닙니다.";
    for(const [direction,key] of [["download","download_mbps"],["upload","upload_mbps"]]){
      const el=$(`network-${direction}`),value=valid?network[key]:null;
      el.textContent=number(value,2);
      const load=finite(value)&&finite(network.link_mbps)&&network.link_mbps>0?value/network.link_mbps*100:null;
      setLevel(el,load,[70,90],"어댑터 연결 속도 대비 송수신 비율(%)");
    }
    $("network-note").textContent=stale?"새 측정값을 기다리고 있습니다.":network.status==="disconnected"?"사용 가능한 기본 경로를 확인할 수 없습니다.":"실시간 송수신 · 인터넷 최대속도 테스트와 다릅니다.";
  }
  function renderTemperatures(sys={},gpu=null,stale=false){
    const temperatures=sys.temperatures||{};
    // Sensor identities and DXGI identities are separate. Never attach a
    // temperature to the busiest GPU by guessing its name or enumeration order.
    const gpuReadings=stale?[]:[
      ...rows(temperatures.gpu?.sensors).filter(s=>finite(s.temp_c)),
      ...rows(sys.gpus).filter(g=>finite(g.temp_c)).map(g=>({name:`${g.name||"GPU"} · ${g.temp_source||g.source||"장치 온도"}`,temp_c:g.temp_c}))
    ];
    const gpuTemperature=gpuReadings.length?{
      temp_c:Math.max(...gpuReadings.map(s=>s.temp_c)),status:"ok",
      details:`측정 가능한 GPU 센서 중 최고 온도 · ${gpuReadings.map(s=>`${s.name}: ${number(s.temp_c)}°C`).join(" · ")}`
    }:temperatures.gpu||{temp_c:null,details:"읽을 수 있는 GPU 온도 센서가 없습니다."};
    const sensors={cpu:temperatures.cpu,ram:temperatures.ram,disk:temperatures.disk,gpu:gpuTemperature};
    $("gpu-temperature-sensors").innerHTML=gpuReadings.length?gpuReadings.map(s=>`<div class="device-line"><span>${safe(s.name)}</span><strong>${number(s.temp_c)}°C</strong></div>`).join(""):`<p class="subtle">${safe(stale?"온도 갱신 지연":gpuTemperature.details)}</p>`;
    const names={cpu:"CPU",gpu:"GPU",ram:"메모리",disk:"디스크"};
    Object.entries(sensors).forEach(([key,sensor])=>{
      const el=$(`${key}-temperature`),measured=!stale&&finite(sensor?.temp_c);
      const waiting=["starting","waiting","pending"].includes(sensor?.status);
      el.textContent=stale?"온도 갱신 지연":measured?`${key==="gpu"?"최고 온도":"온도"} ${number(sensor.temp_c,0)}°C`:waiting?"온도 확인 중":"온도 확인 불가";
      el.classList.toggle("unavailable",!measured);
      const limits=thermalLimits[key],level=levelFor(measured?sensor.temp_c:null,...limits);
      el.dataset.level=level;
      el.title=stale?"새 온도 측정값을 기다리고 있습니다. 이전 온도는 표시하지 않습니다.":sensor?.details||`${names[key]}에서 읽을 수 있는 온도 센서가 없습니다.`;
      if(measured)el.title+=` · ${levelNames[level]} · 표시 참고 기준: 노랑 ${limits[0]}°C / 빨강 ${limits[1]}°C`;
      el.setAttribute("aria-label",`${names[key]} ${el.textContent}${measured&&sensor?.details?` · ${sensor.details}`:""}`);
    });
  }

  async function request(path){const controller=new AbortController();const timer=window.setTimeout(()=>controller.abort(),5000);try{const res=await fetch(path,{cache:"no-store",signal:controller.signal});const data=await res.json();if(!res.ok){const error=new Error(`HTTP ${res.status}`);if(res.status===503&&data?.status==="starting"){error.starting=true;error.collectorError=data.error||null;}throw error;}return data;}finally{window.clearTimeout(timer);}}
  function nextDeadlineAfter(deadline,now){const target=deadline+1000;return target>now?target:target+(Math.floor((now-target)/1000)+1)*1000;}
  async function pollSnapshot(){
    try{state.snapshot=await request("/api/snapshot");state.lastSuccess=Date.now();appendLiveSample(state.snapshot);renderSnapshot();}
    catch(e){if(state.snapshot){renderTemperatures({},null,true);renderUsage({},null,true);renderNetwork({},true);}$("live-status").className="live-status waiting";if(e.starting){$("live-status").innerHTML=e.collectorError?"<i></i>수집 확인 필요":"<i></i>첫 측정 준비 중";$("last-update").textContent=state.lastSuccess?`마지막 수신 ${time(state.lastSuccess/1000,true)}`:"장치 정보를 확인하고 있습니다";$("errors-banner").hidden=!e.collectorError&&!state.snapshot;$("errors-banner").textContent=e.collectorError?`첫 측정 준비 중 확인이 필요합니다: ${String(e.collectorError)}`:state.snapshot?"새 측정을 준비 중입니다. 표시된 수치는 이전에 받은 마지막 값입니다.":"";}else{$("live-status").innerHTML="<i></i>연결 확인 필요";$("last-update").textContent=state.lastSuccess?`마지막 수신 ${time(state.lastSuccess/1000,true)}`:"서버에 연결할 수 없습니다";$("errors-banner").hidden=false;$("errors-banner").textContent="실시간 수집 서버 응답이 없습니다. 표시된 수치는 마지막으로 받은 값이며 새 측정값이 아닙니다. 연결을 자동으로 다시 확인합니다.";}}
    finally{const now=performance.now();snapshotDeadline=nextDeadlineAfter(snapshotDeadline,now);window.setTimeout(pollSnapshot,Math.max(0,snapshotDeadline-now));}
  }
  async function pollHistory(){await loadHistory();window.setTimeout(pollHistory,30000);}
  async function loadHistory(){
    const token=++state.historyRequest;
    try{const value=await request(`/api/history?minutes=${state.minutes}`);if(token!==state.historyRequest)return;state.history=value;renderHistory();}
    catch(e){if(token!==state.historyRequest)return;if(state.chartMode!=="live")$("chart-caption").textContent="기록을 불러오지 못했습니다. 자동으로 다시 확인합니다.";}
  }
  function renderSnapshot(){
    const s=state.snapshot,sys=s.system||{},gpus=rows(sys.gpus),gpu=peakGpu(gpus);
    $("live-status").className=s.stale?"live-status waiting":"live-status";$("live-status").innerHTML=s.stale?"<i></i>측정 지연":"<i></i>실시간 수집 중";$("last-update").textContent=`${time(s.ts,true)} 업데이트`;
    $("cpu-value").textContent=number(sys.cpu_pct);$("cpu-context").textContent=finite(sys.cpu_threads)?`${sys.cpu_threads}개 논리 프로세서`:"전체 처리 용량";$("cpu-detail").textContent=finite(sys.cpu_peak_core_pct)?`가장 바쁜 논리 프로세서 ${percent(sys.cpu_peak_core_pct)}`:"PC 전체 처리 용량 기준";
    $("gpu-value").textContent=number(gpu?.util_pct);$("gpu-context").textContent=gpus.length>1?`${gpus.length}개 중 높은 사용률`:gpu?"장치 사용률":"미측정";$("gpu-context").title=gpu?.name||"";$("gpu-detail").textContent=gpu?`전용 ${memory(gpu.vram_used_gb)} / ${memory(gpu.vram_total_gb)}`:"Windows에서 GPU 정보를 읽지 못했습니다";$("gpu-detail").title=gpu?`${gpu.name} · ${gpu.source||"출처 미확인"} · ${finite(gpu.util_pct)?"사용률 측정 중":"사용률 미측정"}`:"GPU 정보 미측정";
    $("ram-value").textContent=number(sys.ram_pct);$("ram-detail").textContent=`${number(sys.ram_used_gb)} / ${number(sys.ram_total_gb)} GiB · 여유 ${number(sys.ram_available_gb)} GiB`;
    $("disk-value").textContent=number(sum(sys.disk_read_mbps,sys.disk_write_mbps),1);$("disk-detail").textContent=`활동 ${percent(sys.disk_busy_pct)} · 읽기 ${number(sys.disk_read_mbps,1)} · 쓰기 ${number(sys.disk_write_mbps,1)} MiB/s`;$("disk-detail").title=$("disk-detail").textContent;
    renderTemperatures(sys,gpu,s.stale===true);
    renderUsage(sys,gpu,s.stale===true);renderHardware(sys,gpu);renderNetwork(sys.network,s.stale===true);
    renderInsights();renderDevices();renderVoice();renderRecording();renderTable();renderChart();
    if(state.selectedGroup)renderDialog();
    const errors=rows(s.errors).slice();if(s.stale)errors.unshift("새 측정값이 지연되어 마지막 수신값을 표시하고 있습니다");$("errors-banner").hidden=!errors.length;
    $("errors-banner").textContent=errors.length?`일부 항목 수집 안내: ${errors.map(e=>typeof e==="string"?e:e.message||e.detail||"일부 측정 정보 없음").join(" · ")}`:"";
  }
  function renderRecording(){
    const r=state.snapshot?.recording||{};
    $("recording-detail").innerHTML=`<i class="record-dot"></i>로컬 기록 ${finite(r.samples)?`${number(r.samples,0)}회`:"준비 중"}${finite(r.started_at)?` · ${date(r.started_at)}부터`:""}`;
    $("retention-days").textContent=finite(r.retention_days)?r.retention_days:7;
  }
  function renderInsights(){
    const list=rows(state.snapshot?.insights);
    $("insights").innerHTML=list.length?list.map(i=>`<div class="insight ${["watch","warn"].includes(i.level)?i.level:""}"><span class="insight-icon">${i.level==="info"?"i":"!"}</span><div><h3>${safe(i.title)}</h3><p>${safe(i.detail)}</p></div></div>`).join(""):"<div class='insight'><span class='insight-icon'>i</span><div><h3>관측 중</h3><p>업그레이드 판단에 필요한 기록을 모으고 있습니다. 순간 사용률만으로 성능 부족을 단정하지 않습니다.</p></div></div>";
  }
  function renderDevices(){
    const sys=state.snapshot?.system||{};
    $("gpu-devices").innerHTML=rows(sys.gpus).length?rows(sys.gpus).map(g=>`<div class="gpu-device"><div class="device-name">${safe(g.name||"GPU")}</div><div class="device-line"><span>전용 메모리</span><strong>${memory(g.vram_used_gb)} / ${memory(g.vram_total_gb)}</strong></div><div class="capacity-bar ${g.vram_pct>90?"warning":""}"><i style="width:${clamp(g.vram_pct)}%"></i></div>${finite(g.shared_used_gb)||finite(g.shared_total_gb)?`<div class="device-line"><span>공유 메모리</span><strong>${memory(g.shared_used_gb)} / ${memory(g.shared_total_gb)}</strong></div>`:""}<div class="device-line"><span>사용률 ${percent(g.util_pct)}${finite(g.temp_c)?` · ${number(g.temp_c,0)}°C`:""}</span><span title="측정 출처: ${safe(g.source||"미확인")}">${String(g.source||"").includes("PDH")?"최고 부하 엔진":"장치 전체"}</span></div></div>`).join(""):"<p class='subtle'>GPU 정보 미측정 · CPU·메모리 기록은 계속됩니다.</p>";
    $("disk-devices").innerHTML=rows(sys.disks).map(d=>`<div class="disk-item"><div class="device-name">${safe(d.drive)} <span class="device-label">${safe(d.label||"")}</span></div><div class="device-line"><span>사용 ${percent(d.used_pct)}</span></div><div class="capacity-bar ${d.used_pct>90?"warning":""}"><i style="width:${clamp(d.used_pct)}%"></i></div><div class="device-line"><span>${number(d.free_gb,0)} / ${number(d.total_gb,0)} GiB 여유</span></div></div>`).join("")||"<p class='subtle'>저장 공간 미측정</p>";
  }
  function renderVoice(){
    const v=state.snapshot?.voice||{};const labels={running:"생성 중",complete:"생성 완료",completed:"생성 완료",stopped:"중지 상태",paused:"일시 중지",error:"확인 필요",unknown:"상태 미확인",unavailable:"상태 미확인",missing:"상태 미확인",idle:"대기"};
    const available=v.available===true;$("voice-panel").hidden=!available;$("device-details").classList.toggle("has-voice",available);if(!available)return;
    const stages={tokens:"음성 토큰 생성",waveform:"음성 파형 생성",qa:"발음·품질 검사","qa-medium":"추가 품질 검사",prepare:"작업 준비",install:"통과 음성 적용",finish:"파일 마무리",finalize:"완료 기록 정리"};
    $("voice-state").textContent=labels[v.status]||v.status||"상태 확인 중";
    if(!finite(v.finished)||!finite(v.total)){$("voice-content").innerHTML="<p class='subtle'>음성 작업 상태 기록을 확인할 수 없습니다. 자원 수집은 계속됩니다.</p>";return;}
    const ratio=v.total>0?v.finished/v.total*100:0;
    $("voice-content").innerHTML=`<div class="voice-progress-number">${number(v.finished,0)}<span>/ ${number(v.total,0)}개</span></div><div class="capacity-bar"><i style="width:${clamp(ratio)}%"></i></div><div class="voice-stage"><span>${safe(stages[v.stage]||v.stage||"단계 미확인")}</span><strong>${percent(ratio)}</strong></div><p class="subtle">추가 검토 ${finite(v.review)?`${number(v.review,0)}개`:"미측정"} · 기존 작업 상태만 읽습니다</p>`;
  }
  function renderHistory(){
    const h=state.history,c=h?.coverage||{};
    $("pressure-summary").innerHTML=`<span>저장 기록 ${rangeLabel()} · 관측 구간 중 높은 부하 비율</span><span>CPU ≥85% <b>${percent(c.cpu_busy_share)}</b></span><span>GPU ≥90% <b>${percent(c.gpu_busy_share)}</b></span><span>RAM ≥90% <b>${percent(c.ram_pressure_share)}</b></span>`;
    $("export-system").href=`/api/export.csv?kind=system&minutes=${state.minutes}`;$("export-groups").href=`/api/export.csv?kind=groups&minutes=${state.minutes}`;
    renderChart();if(state.mode==="history")renderTable();
  }
  function rangeLabel(){return state.minutes===60?"1시간":state.minutes===1440?"24시간":"7일";}
  function appendLiveSample(s){
    if(!finite(s.ts)||s.stale)return false;
    const previous=state.livePoints[state.livePoints.length-1];if(previous&&s.ts<=previous.ts)return false;
    const sys=s.system||{},gpu=peakGpu(sys.gpus);
    state.livePoints.push({ts:s.ts,cpu_pct:sys.cpu_pct,ram_pct:sys.ram_pct,gpu_pct:gpu?.util_pct??null,vram_pct:gpu?.vram_pct??null,disk_read_mbps:sys.disk_read_mbps,disk_write_mbps:sys.disk_write_mbps});
    state.livePoints=state.livePoints.filter(p=>p.ts>=s.ts-120).slice(-240);return true;
  }
  function recentLivePoints(){const points=state.livePoints;if(!points.length)return [];const cutoff=points[points.length-1].ts-60;return points.filter(p=>p.ts>=cutoff);}
  function getChartPoints(){
    if(state.chartMode==="live")return recentLivePoints();
    let points=rows(state.history?.system).filter(p=>finite(p.ts)).slice();
    points.sort((a,b)=>a.ts-b.ts);if(points.length){const cutoff=points[points.length-1].ts-state.minutes*60;points=points.filter(p=>p.ts>=cutoff);}return points;
  }
  function chartDomain(points){const last=points[points.length-1].ts;return state.chartMode==="live"?{first:last-60,span:60}:{first:points[0].ts,span:Math.max(last-points[0].ts,30)};}
  function pathFor(points,key,xy,maxGap=Infinity){let d="",prev=null;for(const p of points){if(!finite(p[key])){prev=null;continue;}const [x,y]=xy(p,key);const gap=prev&&p.ts-prev.ts>maxGap;d+=`${prev&&!gap?"L":"M"}${x.toFixed(2)},${y.toFixed(2)} `;prev=p;}return d;}
  function renderChart(){
    const points=getChartPoints();state.chartPoints=points;const W=760,H=230,L=40,R=17,T=14,B=29,pw=W-L-R,ph=H-T-B;
    const coverage=state.history?.coverage||{};
    $("chart-caption").textContent=state.chartMode==="live"?`실시간 60초 · 신규 측정 ${points.length}개 · 누적 분석·CSV는 1시간 기록`:`선택 범위 ${rangeLabel()} · 실제 관측 ${elapsed(coverage.duration_minutes)} · ${finite(coverage.samples)?number(coverage.samples,0):"—"}개 요약 기록`;
    let svg="";for(let i=0;i<=4;i++){const v=i*25,y=T+ph*(1-v/100);svg+=`<line x1="${L}" x2="${W-R}" y1="${y}" y2="${y}" stroke="#2b3646" stroke-width=".7"/><text x="${L-9}" y="${y+3}" fill="#7f91aa" font-size="9" text-anchor="end">${v}${v===100?"%":""}</text>`;}
    $("chart-empty").hidden=points.length>0;
    if(points.length){const {first,span}=chartDomain(points);const xy=(p,k)=>[L+(p.ts-first)/span*pw,T+(100-clamp(p[k]))/100*ph];
      for(let i=0;i<=4;i++){const x=L+pw*i/4,ts=first+span*i/4;svg+=`<text x="${x}" y="${H-8}" fill="#7f91aa" font-size="9" text-anchor="${i===0?"start":i===4?"end":"middle"}">${safe(state.minutes===10080&&span>86400?new Date(ts*1000).toLocaleDateString("ko-KR",{month:"numeric",day:"numeric"}):time(ts,state.chartMode==="live"))}</text>`;}
      const spacing=points.length>1?span/(points.length-1):3;
      for(const key of state.series){const d=pathFor(points,key,xy,state.chartMode==="live"?3.5:Math.max(30,spacing*4));svg+=`<path d="${d}" fill="none" stroke="${colors[key]}" stroke-width="1.7" stroke-linejoin="round" stroke-linecap="round"/>`;const valid=points.filter(p=>finite(p[key]));if(valid.length){const [x,y]=xy(valid[valid.length-1],key);svg+=`<circle cx="${x}" cy="${y}" r="2.5" fill="${colors[key]}"/>`;}}
      svg+='<line id="chart-cursor" x1="0" x2="0" y1="14" y2="201" stroke="#9cabc0" stroke-dasharray="3 4" opacity="0"/>';
    }
    $("history-chart").innerHTML=svg;
    const sparkPoints=recentLivePoints();for(const [id,key] of [["cpu","cpu_pct"],["gpu","gpu_pct"],["ram","ram_pct"],["disk","disk_total"]]){const data=sparkPoints.map(p=>({...p,disk_total:sum(p.disk_read_mbps,p.disk_write_mbps)}));const max=key==="disk_total"?Math.max(1,...data.map(p=>finite(p[key])?p[key]:0)):100;const last=data.length?data[data.length-1].ts:0;const xy=(p,k)=>[(p.ts-last+60)/60*180,58-(finite(p[k])?p[k]/max:0)*49];const d=pathFor(data,key,xy,3.5);$(id+"-spark").innerHTML=`<svg viewBox="0 0 180 64" preserveAspectRatio="none"><path d="${d}" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>`;}
  }
  const liveColumns=[{key:"name",label:"프로그램"},{key:"cpu_pct",label:"CPU"},{key:"ram_gb",label:"메모리"},{key:"gpu_pct",label:"GPU"},{key:"io_total",label:"I/O 합계"},{key:"process_count",label:"프로세스"}];
  const historyColumns=[{key:"name",label:"프로그램"},{key:"cpu_avg",label:"CPU 평균"},{key:"cpu_peak",label:"CPU 최고"},{key:"ram_peak_gb",label:"메모리 최고"},{key:"gpu_avg",label:"GPU 평균"},{key:"gpu_peak",label:"GPU 최고"},{key:"io_total_gb",label:"누적 I/O"}];
  function sortRows(input,sort){return [...input].sort((a,b)=>{const av=a[sort.key],bv=b[sort.key];if(sort.key==="name")return String(av||"").localeCompare(String(bv||""),"ko")*sort.dir;const an=finite(av),bn=finite(bv);if(!an&&!bn)return String(a.name||"").localeCompare(String(b.name||""),"ko");if(!an)return 1;if(!bn)return -1;return (av-bv)*sort.dir||String(a.name||"").localeCompare(String(b.name||""),"ko");});}
  function bar(value,color){return `style="--bar:${clamp(value)}%;--fill-color:${color}"`;}
  function iconFor(g){const name=String(g.name||"").toLowerCase();if(/음성|voice|cosy|마린|my sia/.test(name))return {label:"VO",kind:"voice"};if(/codex|chatgpt|claude/.test(name))return {label:"AI",kind:"ai"};if(/chrome/.test(name))return {label:"CH",kind:""};if(/python/.test(name))return {label:"PY",kind:""};return {label:String(g.name||"?").slice(0,2).toUpperCase(),kind:""};}
  function renderTable(){
    const live=state.mode==="live",cols=live?liveColumns:historyColumns,sort=state.sort[state.mode];
    $("program-thead").innerHTML=`<tr>${cols.map(c=>`<th scope="col"${sort.key===c.key?` aria-sort="${sort.dir===1?"ascending":"descending"}"`:""}><button data-sort="${c.key}" class="${sort.key===c.key?"sorted":""}">${c.label}<span class="sort-arrow">${sort.key===c.key?(sort.dir===1?"▲":"▼"):""}</span></button></th>`).join("")}</tr>`;
    let list=rows(live?state.snapshot?.groups:state.history?.groups).map(g=>({...g,io_total:sum(g.io_read_mbps,g.io_write_mbps)}));const total=list.length;const needle=state.search.trim().toLowerCase();if(needle)list=list.filter(g=>String(g.name||"").toLowerCase().includes(needle));list=sortRows(list,sort);
    $("program-count").textContent=`${list.length}${needle?` / ${total}`:""}개 프로그램${live?" · 선택하여 상세 확인":` · ${rangeLabel()} 저장 기록 기준`}`;
    $("program-tbody").innerHTML=list.length?list.map(g=>{const icon=iconFor(g);const name=`<span class="program-icon ${icon.kind}">${safe(icon.label)}</span><span><span class="process-name-text">${safe(g.name||"알 수 없음")}</span><span class="process-subtitle">${live?`${number(g.process_count,0)}개 프로세스${g.gpu_engine?` · ${safe(engineName(g.gpu_engine))}`:""}`:`관측 ${number(g.observed_samples,0)}회`}</span></span>`;
      if(live)return `<tr><td><button class="process-name" data-group="${safe(g.id)}">${name}<span class="group-arrow" aria-hidden="true">›</span></button></td><td class="cpu-cell value-cell" ${bar(g.cpu_pct,"#82a8ff16")}>${percent(g.cpu_pct)}</td><td>${memory(g.ram_gb)}</td><td class="gpu-cell value-cell" ${bar(g.gpu_pct,"#49dac31a")}>${percent(g.gpu_pct)}</td><td>${mbps(g.io_total)}</td><td>${number(g.process_count,0)}</td></tr>`;
      return `<tr><td><div class="process-name">${name}</div></td><td class="cpu-cell">${percent(g.cpu_avg)}</td><td>${percent(g.cpu_peak)}</td><td>${memory(g.ram_peak_gb)}</td><td class="gpu-cell">${percent(g.gpu_avg)}</td><td>${percent(g.gpu_peak)}</td><td>${gb(g.io_total_gb)}</td></tr>`;
    }).join(""):`<tr><td colspan="${cols.length}" class="table-empty">${needle?"검색 조건에 맞는 프로그램이 없습니다.":live?"첫 프로세스 측정을 기다리고 있습니다.":"이 기간에 저장된 프로그램 기록이 없습니다."}</td></tr>`;
    $("table-summary").textContent=live?"프로그램 이름을 선택하면 개별 프로세스의 사용량을 볼 수 있습니다.":`실제 관측 ${elapsed(state.history?.coverage?.duration_minutes)}. 프로그램별 평균은 해당 프로그램이 관측된 표본을 기준으로 합니다. 짧은 관측 결과를 장기 패턴으로 해석하지 마세요.`;
  }
  function renderDialog(){
    const g=rows(state.snapshot?.groups).find(g=>String(g.id)===state.selectedGroup);if(!g){$("dialog-subtitle").textContent="현재 목록에서 사라졌습니다. 프로세스가 종료되었거나 그룹이 바뀌었을 수 있습니다.";return;}
    $("dialog-title").textContent=g.name||"프로세스 상세";$("dialog-subtitle").textContent=`${number(g.process_count,0)}개 프로세스 · ${time(state.snapshot.ts,true)} 측정${g.gpu_engine?` · ${engineName(g.gpu_engine)}`:""}`;
    $("dialog-stats").innerHTML=[["CPU",percent(g.cpu_pct)],["메모리",memory(g.ram_gb)],["GPU",percent(g.gpu_pct)],["I/O",mbps(sum(g.io_read_mbps,g.io_write_mbps))]].map(([label,v])=>`<div class="dialog-stat"><span>${label}</span><strong>${v}</strong></div>`).join("");
    $("process-tbody").innerHTML=rows(g.processes).sort((a,b)=>(b.cpu_pct||0)-(a.cpu_pct||0)).map(p=>`<tr><td>${safe(p.name||"알 수 없음")}<div class="process-subtitle">PID ${safe(p.pid)}${p.script?` · ${safe(p.script)}`:""}</div></td><td class="cpu-cell">${percent(p.cpu_pct)}</td><td>${memory(p.ram_gb)}</td><td class="gpu-cell">${percent(p.gpu_pct)}</td><td>${mbps(sum(p.io_read_mbps,p.io_write_mbps))}</td></tr>`).join("")||"<tr><td colspan='5' class='table-empty'>개별 프로세스 상세 정보가 없습니다.</td></tr>";
  }

  document.querySelectorAll("[data-minutes],[data-live-range]").forEach(b=>b.addEventListener("click",()=>{state.chartMode=b.hasAttribute("data-live-range")?"live":"history";state.minutes=state.chartMode==="live"?60:Number(b.dataset.minutes);document.querySelectorAll("[data-minutes],[data-live-range]").forEach(x=>{const active=x===b;x.classList.toggle("active",active);x.setAttribute("aria-pressed",String(active));});state.history=null;$("chart-caption").textContent="선택한 기간의 기록을 불러오는 중입니다.";$("export-system").href=`/api/export.csv?kind=system&minutes=${state.minutes}`;$("export-groups").href=`/api/export.csv?kind=groups&minutes=${state.minutes}`;renderChart();if(state.mode==="history")renderTable();loadHistory();}));
  document.querySelectorAll("[data-series]").forEach(b=>b.addEventListener("click",()=>{const k=b.dataset.series;if(state.series.has(k))state.series.delete(k);else state.series.add(k);b.classList.toggle("off",!state.series.has(k));b.setAttribute("aria-pressed",String(state.series.has(k)));renderChart();}));
  document.querySelectorAll("[data-mode]").forEach(b=>b.addEventListener("click",()=>{state.mode=b.dataset.mode;document.querySelectorAll("[data-mode]").forEach(x=>{x.classList.toggle("active",x===b);x.setAttribute("aria-pressed",String(x===b));});renderTable();}));
  $("program-search").addEventListener("input",e=>{state.search=e.target.value;renderTable();});
  $("program-thead").addEventListener("click",e=>{const b=e.target.closest("[data-sort]");if(!b)return;const sort=state.sort[state.mode];if(sort.key===b.dataset.sort)sort.dir*=-1;else{sort.key=b.dataset.sort;sort.dir=sort.key==="name"?1:-1;}renderTable();});
  $("program-tbody").addEventListener("click",e=>{const b=e.target.closest("[data-group]");if(!b)return;state.selectedGroup=b.dataset.group;renderDialog();$("process-dialog").showModal();});
  $("dialog-close").addEventListener("click",()=>$("process-dialog").close());$("process-dialog").addEventListener("close",()=>{state.selectedGroup=null;});$("process-dialog").addEventListener("click",e=>{if(e.target===$("process-dialog")){const rect=e.target.getBoundingClientRect();if(e.clientX<rect.left||e.clientX>rect.right||e.clientY<rect.top||e.clientY>rect.bottom)e.target.close();}});
  $("history-chart").addEventListener("pointermove",e=>{const points=state.chartPoints;if(!points.length)return;const rect=e.currentTarget.getBoundingClientRect(),x=(e.clientX-rect.left)/rect.width*760,{first,span}=chartDomain(points),target=first+Math.max(0,Math.min(1,(x-40)/703))*span;let nearest=points[0];for(const p of points){if(Math.abs(p.ts-target)<Math.abs(nearest.ts-target))nearest=p;}const tooltip=$("chart-tooltip");tooltip.hidden=false;tooltip.innerHTML=`<b>${safe(state.chartMode==="live"?time(nearest.ts,true):date(nearest.ts))}</b>${[...state.series].map(k=>`<span style="color:${colors[k]}">${seriesNames[k]} ${percent(nearest[k])}</span>`).join("")}`;const wrap=$("chart-wrap").getBoundingClientRect();tooltip.style.left=`${Math.max(8,Math.min(wrap.width-155,e.clientX-wrap.left+12))}px`;const cursor=$("chart-cursor");if(cursor){const cx=40+(nearest.ts-first)/span*703;cursor.setAttribute("x1",cx);cursor.setAttribute("x2",cx);cursor.setAttribute("opacity",".55");}});
  $("history-chart").addEventListener("pointerleave",()=>{$("chart-tooltip").hidden=true;const cursor=$("chart-cursor");if(cursor)cursor.setAttribute("opacity","0");});
  document.addEventListener("click",e=>{const menu=document.querySelector(".export-menu");if(menu.open&&!menu.contains(e.target))menu.open=false;});
  renderTable();renderChart();pollSnapshot();pollHistory();
})();
