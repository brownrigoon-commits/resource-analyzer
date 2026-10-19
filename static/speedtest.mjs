import CloudflareSpeedTest from './vendor/cloudflare-speedtest-1.14.2.mjs';

export function createConfig() {
  return {
    autoStart: false,
    downloadApiUrl: 'https://speed.cloudflare.com/__down',
    uploadApiUrl: 'https://speed.cloudflare.com/__up',
    // Transfers are required for the test; optional results telemetry is not.
    logMeasurementApiUrl: null,
    logAimApiUrl: null,
    includeCredentials: false,
    measureDownloadLoadedLatency: false,
    measureUploadLoadedLatency: false,
    bandwidthFinishRequestDuration: 1000,
    bandwidthAbortRequestDuration: 12000,
    measurements: [
      { type: 'latency', numPackets: 4 },
      ...['download', 'upload'].flatMap(type => [
        { type, bytes: 100_000, count: 1, bypassMinDuration: true },
        { type, bytes: 1_000_000, count: 2 },
        { type, bytes: 10_000_000, count: 3 },
        { type, bytes: 25_000_000, count: 2 },
      ]),
    ],
  };
}

export function mountSpeedTest({doc = globalThis.document, Engine = CloudflareSpeedTest,
  timers = globalThis, locks = globalThis.navigator?.locks, now = () => new Date()} = {}) {
  const $ = id => doc.getElementById(id);
  const startButton = $('speedtest-start');
  if (!startButton) return;
  const cancelButton = $('speedtest-cancel'), panel = $('speedtest-panel');
  const status = $('speedtest-status'), progress = $('speedtest-progress');
  const valid = value => typeof value === 'number' && Number.isFinite(value) && value > 0;
  const format = value => valid(value) ? value.toLocaleString('ko-KR', {maximumFractionDigits: 1, minimumFractionDigits: 1}) : '—';
  let active = false, waiting = false, generation = 0, finishCurrent = null;

  function showResults(summary = {}) {
    $('speedtest-download').textContent = format(valid(summary.download) ? summary.download / 1_000_000 : null);
    $('speedtest-upload').textContent = format(valid(summary.upload) ? summary.upload / 1_000_000 : null);
    $('speedtest-latency').textContent = format(summary.latency);
  }

  function run() {
    return new Promise(resolve => {
      waiting = false;
      active = true;
      const id = ++generation;
      let engine = null, timeout = null;
      panel.hidden = false;
      panel.dataset.state = 'running';
      panel.setAttribute('aria-busy', 'true');
      startButton.disabled = true;
      startButton.textContent = '측정 중…';
      cancelButton.hidden = false;
      progress.hidden = false;
      status.textContent = '측정 서버 연결 중 · 표시값은 잠정 결과입니다.';
      $('speedtest-time').textContent = '';
      showResults();

      const finish = (message, state) => {
        if (!active || id !== generation) return;
        active = false;
        timers.clearTimeout(timeout);
        engine?.pause();
        finishCurrent = null;
        panel.dataset.state = state;
        panel.setAttribute('aria-busy', 'false');
        startButton.disabled = false;
        startButton.textContent = '다시 측정';
        cancelButton.hidden = true;
        progress.hidden = true;
        status.textContent = message;
        resolve();
      };
      finishCurrent = finish;
      try {
        engine = new Engine(createConfig());
        engine.onPhaseChange = ({measurement}) => {
          if (!active || id !== generation) return;
          const phase = {latency: '응답 지연', download: '다운로드', upload: '업로드'}[measurement.type] || '인터넷 속도';
          status.textContent = `${phase} 측정 중 · 표시값은 잠정 결과입니다.`;
        };
        engine.onResultsChange = () => {
          if (active && id === generation) showResults(engine.results.getSummary());
        };
        engine.onFinish = results => {
          if (!active || id !== generation) return;
          const summary = results.getSummary();
          showResults(summary);
          if (!valid(summary.download) || !valid(summary.upload)) {
            finish('일부 속도를 측정하지 못했습니다. 표시값은 부분 결과이며 다시 시도할 수 있습니다.', 'error');
            return;
          }
          $('speedtest-time').textContent = `${now().toLocaleString('ko-KR', {hour12: false})} 측정`;
          finish('측정 완료 · 현재 연결에서 Cloudflare 서버까지의 속도입니다.', 'complete');
        };
        engine.onError = () => finish('측정 서버 연결이 실패했거나 응답이 지연됐습니다. 표시값은 부분 결과입니다.', 'error');
        timeout = timers.setTimeout(() => finish('측정 시간이 초과됐습니다. 표시값은 부분 결과이며 다시 시도할 수 있습니다.', 'error'), 60000);
        engine.play();
      } catch {
        finish('속도 테스트를 시작하지 못했습니다. 잠시 후 다시 시도해 주세요.', 'error');
      }
    });
  }

  async function start() {
    if (active || waiting) return;
    waiting = true;
    startButton.disabled = true;
    try {
      if (locks) {
        await locks.request('resource-analyzer-internet-speedtest', {ifAvailable: true}, async lock => {
          if (lock) await run();
          else {
            waiting = false;
            startButton.disabled = false;
            panel.hidden = false;
            status.textContent = '다른 탭에서 속도를 측정 중입니다. 완료 후 다시 눌러 주세요.';
          }
        });
      } else await run();
    } catch {
      waiting = false;
      startButton.disabled = false;
      panel.hidden = false;
      status.textContent = '속도 테스트를 시작하지 못했습니다. 다시 시도해 주세요.';
    }
  }

  startButton.addEventListener('click', start);
  cancelButton.addEventListener('click', () => finishCurrent?.('측정을 중지했습니다. 표시값은 중지 전의 부분 결과입니다.', 'cancelled'));
  globalThis.addEventListener?.('pagehide', () => finishCurrent?.('측정 중지됨', 'cancelled'));
  return {start, cancel: () => finishCurrent?.('측정 중지됨', 'cancelled')};
}

if (typeof document !== 'undefined') mountSpeedTest();
