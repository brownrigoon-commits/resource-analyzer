const finite = value => typeof value === 'number' && Number.isFinite(value);
const dateTime = value => finite(value) ? new Date(value * 1000).toLocaleString('ko-KR', {
  year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
}) : '시각 미확인';
function programFor(item) {
  if (item.program) return item.program;
  // Older stored records retain their original evidence; don't infer from today's processes.
  const match = String(item.evidence || '').match(/^같은 시점 (?:사용량|논리 I\/O) 상위:\s*(.+)/);
  return match ? match[1].split(' · ')[0].replace(/\s+[\d,.]+(?:%| GiB| MiB\/s).*$/, '') : '미확인';
}

export function renderWarningHistory(doc, data) {
  const items = Array.isArray(data.events) ? data.events : [];
  const status = doc.getElementById('resource-report-status');
  const statusText = `기록 ${data.total ?? 0}건`;
  if (status.textContent !== statusText) status.textContent = statusText;
  const updated = doc.getElementById('resource-report-updated');
  updated.hidden = !data.recording_error && !data.stale && data.collecting;
  updated.textContent = data.recording_error ? '저장 오류 · 일부 기록 누락 가능' : '측정 대기 · 기록 유지';
  doc.getElementById('resource-report-empty').hidden = items.length > 0;
  const list = doc.getElementById('resource-report-list');
  // Avoid losing keyboard focus or scroll position on unchanged history pages.
  const signature = JSON.stringify(items);
  if (list.dataset.signature !== signature) {
    list.dataset.signature = signature;
    list.replaceChildren(...items.map(item => {
      const row = doc.createElement('li'); row.className = 'resource-report-item'; row.dataset.level = item.level;
      const heading = doc.createElement('div'); heading.className = 'resource-report-item-heading';
      const main = doc.createElement('div'); main.className = 'resource-report-heading-main';
      const time = doc.createElement('time'); time.textContent = dateTime(item.started_at);
      const title = doc.createElement('h4'); title.textContent = item.title;
      const badge = doc.createElement('span'); badge.className = 'resource-report-badge';
      badge.textContent = `${item.level === 'high' ? '높음' : '주의'} · 최고 ${item.value}`;
      main.append(time, title); heading.append(main, badge); row.append(heading);
      const activity = doc.createElement('p'); activity.className = 'resource-report-activity';
      const thermal = item.simultaneous || String(item.id).endsWith('-temperature');
      activity.textContent = `${thermal ? '동시 관측' : '상위 프로그램'}: ${programFor(item)} · 작업: ${item.task || '세부 작업 미확인'}${thermal ? ' (발열 원인 미확인)' : ''}`;
      row.append(activity);
      return row;
    }));
  }
  list.hidden = items.length === 0;
}

export function mountWarningHistory({doc, request, timers = globalThis, storage}) {
  let before = null, next = null, generation = 0, requestId = 0, stopped = false, timer = null;
  try { if (storage === undefined) storage = globalThis.localStorage; } catch { storage = null; }
  const toggle = doc.getElementById('resource-report-toggle');
  const body = doc.getElementById('resource-report-body');
  const older = doc.getElementById('resource-report-older');
  const latest = doc.getElementById('resource-report-latest');
  const error = doc.getElementById('resource-report-error');
  const collapsedKey = 'resource-report-collapsed';
  function collapse(value) {
    body.hidden = value;
    toggle.textContent = value ? '기록 펼치기' : '기록 접기';
    toggle.setAttribute('aria-expanded', String(!value));
    try { storage?.setItem(collapsedKey, String(value)); } catch {}
  }
  try { collapse(storage?.getItem(collapsedKey) === 'true'); } catch { collapse(false); }
  toggle.addEventListener('click', () => collapse(!body.hidden));
  async function refresh() {
    if (stopped) return;
    const current = generation;
    const currentRequest = ++requestId;
    try {
      const data = await request(`/api/warning-events?limit=50${before === null ? '' : `&before=${before}`}`);
      if (current !== generation || currentRequest !== requestId || stopped) return;
      renderWarningHistory(doc, data);
      next = data.next_before;
      older.disabled = next == null;
      latest.hidden = before === null;
      error.hidden = !data.recording_error;
      error.textContent = data.recording_error ? `경고 기록 저장에 실패했습니다. 이 구간에 누락이 생길 수 있습니다: ${data.recording_error}` : '';
    } catch {
      if (current !== generation || currentRequest !== requestId || stopped) return;
      // A failed fetch must never erase the history already on screen.
      error.hidden = false;
      error.textContent = '기록 서버 연결을 확인하고 있습니다. 표시된 기록은 마지막 수신값이며 현재 상태는 미확인입니다.';
      doc.getElementById('resource-report-updated').textContent = '연결 확인 중 · 기존 기록 유지';
      doc.getElementById('resource-report-updated').hidden = false;
    }
  }
  older.addEventListener('click', () => { if (next != null) { before = next; generation++; doc.getElementById('resource-report-list').scrollTop = 0; refresh(); } });
  latest.addEventListener('click', () => { before = null; generation++; doc.getElementById('resource-report-list').scrollTop = 0; refresh(); });
  async function poll() { if (stopped) return; await refresh(); if (!stopped) timer = timers.setTimeout(poll, 1000); }
  poll();
  return {refresh, stop() { stopped = true; if (timer !== null) timers.clearTimeout(timer); }, collapse};
}
