// The cards and this report share thresholds and sensor selection.
const finite = value => typeof value === 'number' && Number.isFinite(value);
const rows = value => Array.isArray(value) ? value : [];
const number = (value, digits = 1) => finite(value) ? value.toLocaleString('ko-KR', {
  minimumFractionDigits: digits, maximumFractionDigits: digits,
}) : '—';
export const usageLimits = Object.freeze({cpu: [70, 90], gpu: [70, 90], ram: [80, 90], disk: [70, 90], network: [70, 90]});
export const thermalLimits = Object.freeze({cpu: [80, 85], gpu: [80, 90], ram: [65, 80], disk: [55, 65]});
export const levelFor = (value, watch, high) => !finite(value) ? 'unknown' : value >= high ? 'high' : value >= watch ? 'watch' : 'safe';
const names = {cpu: 'CPU', gpu: 'GPU', ram: '메모리', disk: '디스크', network: '네트워크'};
const levelNames = {watch: '주의', high: '높음'};

export function temperatureReadings(system = {}, stale = false) {
  const temperatures = system.temperatures || {};
  // Temperature identities are independent of the busiest GPU's identity.
  const gpuReadings = stale ? [] : [
    ...rows(temperatures.gpu?.sensors).filter(sensor => finite(sensor.temp_c)),
    ...rows(system.gpus).filter(gpu => finite(gpu.temp_c)).map(gpu => ({
      name: `${gpu.name || 'GPU'} · ${gpu.temp_source || gpu.source || '장치 온도'}`, temp_c: gpu.temp_c,
    })),
  ];
  const gpu = gpuReadings.length ? {
    temp_c: Math.max(...gpuReadings.map(sensor => sensor.temp_c)), status: 'ok',
    details: `측정 가능한 GPU 센서 중 최고 온도 · ${gpuReadings.map(sensor => `${sensor.name}: ${number(sensor.temp_c)}°C`).join(' · ')}`,
  } : temperatures.gpu || {temp_c: null, details: '읽을 수 있는 GPU 온도 센서가 없습니다.'};
  return {gpuReadings, sensors: {cpu: temperatures.cpu, ram: temperatures.ram, disk: temperatures.disk, gpu}};
}

function topPrograms(snapshot, key) {
  if (key === 'gpu_pct' && snapshot.coverage?.process_gpu_available === false) return [];
  return rows(snapshot.groups).filter(group => finite(group[key]) && group[key] > 0)
    .slice().sort((a, b) => b[key] - a[key]).slice(0, 3);
}

function programEvidence(snapshot, resource) {
  const key = {cpu: 'cpu_pct', gpu: 'gpu_pct', ram: 'ram_gb', disk: 'io_total'}[resource];
  const groups = resource === 'disk' ? rows(snapshot.groups).map(group => ({...group,
    io_total: finite(group.io_read_mbps) && finite(group.io_write_mbps) ? group.io_read_mbps + group.io_write_mbps : null,
  })) : rows(snapshot.groups);
  const top = topPrograms({...snapshot, groups}, key);
  if (!top.length) return '해당 자원의 프로그램별 측정값이 없어 사용 프로그램을 특정할 수 없습니다.';
  const unit = resource === 'ram' ? ' GiB' : resource === 'disk' ? ' MiB/s' : '%';
  const detail = top.map(group => {
    const engine = resource === 'gpu' ? String(group.gpu_engine || '').match(/engtype_([a-z0-9_ -]+)/i)?.[1]?.replaceAll('_', ' ') : '';
    return `${group.name || '이름 미확인 프로그램'} ${number(group[key])}${unit}${engine ? ` (${engine})` : ''}`;
  }).join(' · ');
  return `${resource === 'disk' ? '같은 시점 논리 I/O 상위' : '같은 시점 사용량 상위'}: ${detail}`;
}

export function buildResourceReport(snapshot) {
  if (!snapshot || snapshot.stale === true) return {state: 'waiting', items: [], ts: null};
  const system = snapshot.system || {};
  const gpus = rows(system.gpus);
  const gpu = gpus.reduce((best, item) => !best || (finite(item.util_pct) ? item.util_pct : -1) >
    (finite(best.util_pct) ? best.util_pct : -1) ? item : best, null);
  const items = [];
  const add = (id, resource, title, value, limits, unit, evidence, note) => {
    const level = levelFor(value, ...limits);
    if (!['watch', 'high'].includes(level)) return;
    const threshold = limits[level === 'high' ? 1 : 0];
    items.push({id, resource, title, level, value: `${number(value)}${unit}`,
      reason: `현재 ${number(value)}${unit}로 ${levelNames[level]} 표시 기준 ${threshold}${unit} 이상입니다.`,
      evidence, note});
  };
  for (const [key, value] of Object.entries({cpu: system.cpu_pct, gpu: gpu?.util_pct, ram: system.ram_pct, disk: system.disk_busy_pct})) {
    const notes = {
      cpu: '전체 CPU 용량 기준입니다. 높은 사용률만으로 오류나 과열을 뜻하지는 않습니다.',
      gpu: `측정 장치: ${gpu?.name || '이름 미확인 GPU'}. 프로그램별 합계는 장치 사용률과 표본 시점·집계 방식이 다릅니다.`,
      ram: `사용 ${number(system.ram_used_gb)} / ${number(system.ram_total_gb)} GiB · 여유 ${number(system.ram_available_gb)} GiB. 프로그램 작업 집합은 공유 메모리가 중복될 수 있으며 전체에는 OS·캐시도 포함됩니다.`,
      disk: `물리 디스크 읽기 ${number(system.disk_read_mbps)} · 쓰기 ${number(system.disk_write_mbps)} MiB/s. 프로그램의 논리 I/O와 달라 특정 프로그램·드라이브가 원인이라고 확정할 수 없습니다.`,
    };
    add(`${key}-usage`, key, `${names[key]} ${key === 'disk' ? '활동률' : '사용률'}`, value, usageLimits[key], '%', programEvidence(snapshot, key), notes[key]);
  }
  const {sensors, gpuReadings} = temperatureReadings(system);
  for (const [key, sensor] of Object.entries(sensors)) {
    const hottest = key === 'gpu' ? gpuReadings.filter(item => item.temp_c === sensor?.temp_c).map(item => item.name).join(' · ') : '';
    add(`${key}-temperature`, key, `${names[key]} ${key === 'gpu' ? '최고 온도' : '온도'}`, sensor?.temp_c,
      thermalLimits[key], '°C', hottest ? `온도 센서: ${hottest}` : sensor?.details || '장치 온도 센서의 실측값입니다.',
      '표시 참고 기준입니다. 온도만으로 냉각·팬·주변 온도 등 물리적인 상승 원인을 확정할 수 없습니다.');
  }
  const network = system.network || {};
  if (network.kind === 'disconnected') {
    items.push({id: 'network-disconnected', resource: 'network', title: '네트워크 연결', level: 'high', value: '연결 없음',
      reason: '사용 가능한 기본 네트워크 경로가 확인되지 않아 높음으로 표시합니다.',
      evidence: network.name ? `어댑터: ${network.name}` : '기본 경로 측정 결과입니다.',
      note: '케이블·공유기·통신사 중 어느 구간의 문제인지는 이 측정만으로 확정할 수 없습니다.'});
  } else if (network.status === 'ok' && finite(network.link_mbps) && network.link_mbps > 0) {
    for (const [direction, field] of [['다운로드', 'download_mbps'], ['업로드', 'upload_mbps']]) {
      const value = network[field];
      add(`network-${field}`, 'network', `네트워크 ${direction} 사용률`, finite(value) ? value / network.link_mbps * 100 : null,
        usageLimits.network, '%', `${network.name || '현재 어댑터'} · ${direction} ${number(value, 2)} Mbps / 연결 ${number(network.link_mbps, 0)} Mbps`,
        'PC와 공유기 사이 연결 속도 대비 전송량입니다. 인터넷 상품 속도·속도 테스트 결과나 고장 판정이 아닙니다. 프로그램별 네트워크 원인은 수집하지 않습니다.');
    }
  }
  items.sort((a, b) => (b.level === 'high') - (a.level === 'high'));
  return {state: items.length ? 'active' : 'clear', items, ts: snapshot.ts};
}

export function renderResourceReport(doc, snapshot) {
  const report = buildResourceReport(snapshot);
  const panel = doc.getElementById('resource-report');
  if (!panel) return;
  panel.dataset.state = report.state;
  const high = report.items.filter(item => item.level === 'high').length;
  const watch = report.items.length - high;
  doc.getElementById('resource-report-status').textContent = report.state === 'waiting'
    ? '새 측정 대기 중입니다. 이전 경고를 현재 상태로 표시하지 않습니다.'
    : report.items.length ? [high ? `높음 ${high}건` : '', watch ? `주의 ${watch}건` : ''].filter(Boolean).join(' · ')
      : '현재 측정된 항목에 주의·높음 상태가 없습니다. 미측정 항목은 판단에서 제외합니다.';
  doc.getElementById('resource-report-updated').textContent = finite(report.ts)
    ? `${new Date(report.ts * 1000).toLocaleTimeString('ko-KR', {hour12: false})} 측정 · 약 1초마다 갱신` : '측정 연결 확인 중';
  const list = doc.getElementById('resource-report-list');
  const nodes = report.items.map(item => {
    const row = doc.createElement('li');
    row.className = 'resource-report-item'; row.dataset.level = item.level;
    const heading = doc.createElement('div'); heading.className = 'resource-report-item-heading';
    const title = doc.createElement('h4'); title.textContent = item.title;
    const badge = doc.createElement('span'); badge.className = 'resource-report-badge';
    badge.textContent = `${levelNames[item.level]} · ${item.value}`;
    heading.append(title, badge); row.append(heading);
    for (const [className, text] of [['reason', item.reason], ['evidence', item.evidence], ['note', item.note]]) {
      const paragraph = doc.createElement('p'); paragraph.className = `resource-report-${className}`;
      paragraph.textContent = text; row.append(paragraph);
    }
    return row;
  });
  list.replaceChildren(...nodes);
  list.hidden = nodes.length === 0;
}
