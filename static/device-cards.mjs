import {levelFor, usageLimits, thermalLimits} from './resource-report.mjs';

const rows = value => Array.isArray(value) ? value : [];
const finite = value => typeof value === 'number' && Number.isFinite(value);
const fmt = (value, digits=1) => finite(value) ? value.toLocaleString('ko-KR', {maximumFractionDigits:digits,minimumFractionDigits:digits}) : '—';
const safe = value => String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const driveKey = value => String(value||'').replace(/[\\/]+$/, '').toUpperCase();
const maxTemp = sensors => {const values=rows(sensors).map(s=>s.temp_c).filter(finite);return values.length?Math.max(...values):null;};

export function deviceModels(system={}, stale=false) {
  const cards=[];
  for (const gpu of rows(system.gpus)) {
    // A name is only usable when unique in BOTH inventories. Never borrow the
    // hottest temperature from a different adapter. Direct driver readings win.
    const unique=rows(system.gpus).filter(g=>g.name===gpu.name).length===1;
    const extra=unique?rows(system.temperatures?.gpu?.sensors).filter(s=>s.name?.startsWith(gpu.name+' · ')):[];
    const temperature=finite(gpu.temp_c)?gpu.temp_c:null;
    cards.push({id:gpu.id||gpu.adapter_luid||gpu.name,kind:'gpu',label:'GPU',name:gpu.name,
      spec:`전용 메모리 ${fmt(gpu.vram_total_gb)} GiB`,value:stale?null:gpu.util_pct,unit:'%',
      temp:stale?null:temperature, tempDetails:stale?'새 온도 측정 대기':extra.map(s=>`${s.name}: ${fmt(s.temp_c)}°C`).join(' · '),
      detail:`전용 ${fmt(gpu.vram_used_gb)} / ${fmt(gpu.vram_total_gb)} GiB`,
      extra:finite(gpu.shared_used_gb)?`공유 메모리 ${fmt(gpu.shared_used_gb)} GiB`:'',
      sensorExtra:!stale&&extra.length?`보조 센서 최고 ${fmt(maxTemp(extra))}°C`:'',
      source:gpu.source||'장치 사용률'});
  }

  const modules=rows(system.hardware?.memory), sensors=rows(system.temperatures?.ram?.sensors);
  const sameSpec=modules.length>0&&modules.every(m=>m.part===modules[0].part&&m.capacity===modules[0].capacity);
  const types={24:'DDR3',26:'DDR4',34:'DDR5',30:'LPDDR4',35:'LPDDR5'};
  // Identical modules can share their capacity/specification, but their SMBIOS
  // bank order does not establish a DIMM temperature sensor's slot identity.
  const sensorModules=sameSpec&&sensors.length===modules.length&&sensors.every(s=>s.name?.includes(modules[0].part));
  modules.forEach((module,index)=>{
    const matches=sensors.filter(s=>module.part&&s.name?.includes(module.part));
    const sensor=sensorModules?sensors[index]:modules.filter(m=>m.part===module.part).length===1&&matches.length===1?matches[0]:null;
    const slot=sensor?.name?.match(/DIMM #[0-9]+/)?.[0]||[module.bank,module.slot].filter(Boolean).join(' · ')||`모듈 ${index+1}`;
    cards.push({id:sensor?.hardware_id||`ram-${module.bank||''}-${module.slot||''}-${index}`,kind:'ram',label:`메모리 · ${slot}`,
      name:[module.manufacturer,module.part].filter(Boolean).join(' · '),spec:[types[module.type],module.speed?`${module.speed} MT/s`:null].filter(Boolean).join(' · '),
      value:module.capacity/1024**3,unit:'GB',temp:stale?null:sensor?.temp_c,
      detail:'개별 사용률은 Windows에서 제공하지 않습니다',extra:'사용량은 전체 메모리 카드에서 확인',capacityOnly:true});
  });

  for(const disk of rows(system.hardware?.storage)) {
    if(!Number.isInteger(disk.number))continue;
    const id=String(disk.number), io=system.physical_disk_io?.[id];
    const volumes=rows(disk.drives).map(drive=>rows(system.disks).find(d=>driveKey(d.drive)===driveKey(drive))).filter(Boolean);
    const sensors=rows(system.temperatures?.disk?.sensors).filter(s=>new RegExp(`^/(?:hdd|ssd|nvme)/${id}$`).test(s.hardware_id||''));
    const rate=finite(io?.read_mbps)&&finite(io?.write_mbps)?io.read_mbps+io.write_mbps:null;
    cards.push({id:`disk-${id}`,kind:'disk',label:`디스크 ${id} · ${disk.bus==='USB'?'외장':'내장'}`,name:disk.name,
      spec:[rows(disk.drives).join(' · ')||'연결된 드라이브 문자 없음',disk.bus,`${fmt(disk.size/1e12,2)} TB`].filter(Boolean).join(' · '),
      value:stale?null:rate,unit:'MiB/s',usage:stale?null:system.physical_disk_activity?.[id],temp:stale?null:maxTemp(sensors),
      detail:stale?'갱신 지연':`활동 ${fmt(system.physical_disk_activity?.[id])}% · 읽기 ${fmt(io?.read_mbps)} · 쓰기 ${fmt(io?.write_mbps)}`,
      volumes:volumes.map(v=>({drive:driveKey(v.drive),used:v.total_gb-v.free_gb,free:v.free_gb,total:v.total_gb})),
      extra:volumes.length?'': '드라이브 용량 정보 확인 중'});
  }
  return cards;
}

function html(card, spark='') {
  const usage=card.capacityOnly?'unknown':levelFor(card.kind==='disk'?card.usage:card.value,...usageLimits[card.kind]);
  const thermal=levelFor(card.temp,...thermalLimits[card.kind]);
  const volumes=rows(card.volumes).map(v=>`<div class="disk-capacity"><p>${safe(v.drive)} · 사용 ${fmt(v.used)} GiB · 남음 ${fmt(v.free)} GiB</p><div class="capacity-bar ${v.free/v.total<=.1?'warning':''}"><i style="width:${finite(v.total)&&v.total>0?Math.max(0,Math.min(100,v.used/v.total*100)):0}%"></i></div></div>`).join('');
  return `<div class="metric-label"><span><i></i>${safe(card.label)}</span></div><div class="metric-hardware"><div class="metric-model">${safe(card.name)}</div><div class="metric-spec">${safe(card.spec)}</div></div><div class="metric-value"><strong data-level="${usage}">${fmt(card.value)}</strong><span>${safe(card.unit)}</span></div><span class="metric-temperature ${finite(card.temp)?'':'unavailable'}" data-level="${thermal}" title="${safe(card.tempDetails||'이 장치에서 읽은 온도')}">${finite(card.temp)?`온도 ${fmt(card.temp)}°C`:'온도 확인 불가'}</span><div class="sparkline" aria-hidden="true">${spark}</div><p title="${safe(card.source||card.detail)}">${safe(card.detail)}</p>${card.extra?`<p>${safe(card.extra)}</p>`:''}${card.sensorExtra?`<p title="${safe(card.tempDetails)}">${safe(card.sensorExtra)}</p>`:''}${volumes}`;
}

export function mountDeviceCards(doc) {
  const grid=doc.getElementById('metric-grid'), cache=new Map(), points=new Map();
  const ram=doc.getElementById('ram-value').closest('article');
  function render(system={},stale=false,ts=null) {
    const models=deviceModels(system,stale), active=new Set();
    for(const card of models) {
      const key=`${card.kind}:${card.id}`;active.add(key);
      let el=cache.get(key);
      if(!el){el=doc.createElement('article');el.className=`metric-card device-card ${card.kind}`;el.dataset.device=key;cache.set(key,el);}
      const series=points.get(key)||[], last=series.at(-1);
      if(!stale&&finite(ts)&&(!last||last.ts<ts)&&!card.capacityOnly)series.push({ts,value:card.value});
      const recent=series.filter(p=>finite(ts)&&p.ts>=ts-60).slice(-120);points.set(key,recent);
      const max=card.kind==='disk'?Math.max(1,...recent.map(p=>finite(p.value)?p.value:0)):100;
      let path='',previous=null;
      for(const p of recent){if(!finite(p.value)){previous=null;continue;}const x=(p.ts-ts+60)*3,y=58-Math.min(max,p.value)/max*49;path+=`${previous&&p.ts-previous.ts<3.5?'L':'M'}${x.toFixed(1)},${y.toFixed(1)} `;previous=p;}
      el.innerHTML=html(card,`<svg viewBox="0 0 180 64" preserveAspectRatio="none"><path d="${path}" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>`);
      if(card.kind==='gpu')grid.insertBefore(el,ram);else grid.append(el);
    }
    for(const [key,el] of cache)if(!active.has(key)){el.remove();cache.delete(key);points.delete(key);}
  }
  return render;
}
