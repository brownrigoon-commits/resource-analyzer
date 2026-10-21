import test from 'node:test';
import assert from 'node:assert/strict';
import {buildResourceReport, renderResourceReport, temperatureReadings} from './static/resource-report.mjs';

function sample(system = {}, groups = []) {
  return {ts: 1700000000, stale: false, system: {cpu_pct: 10, ram_pct: 40, disk_busy_pct: 5,
    gpus: [{name: 'Synthetic GPU', util_pct: 20}], ...system}, groups};
}
const item = (report, id) => report.items.find(row => row.id === id);

test('normal and unavailable measurements do not create warning reports', () => {
  assert.equal(buildResourceReport(sample()).state, 'clear');
  const report = buildResourceReport(sample({cpu_pct: null, ram_pct: NaN, disk_busy_pct: undefined, gpus: []}));
  assert.deepEqual(report.items, []);
  assert.equal(buildResourceReport(null).state, 'waiting');
});

test('usage thresholds include exact boundaries; report explains the crossing', () => {
  let report = buildResourceReport(sample({cpu_pct: 69.99, ram_pct: 79.99, disk_busy_pct: 69.99}));
  assert.deepEqual(report.items, []);
  report = buildResourceReport(sample({cpu_pct: 70, ram_pct: 80, disk_busy_pct: 90}));
  assert.equal(item(report, 'cpu-usage').level, 'watch');
  assert.match(item(report, 'cpu-usage').reason, /70%/);
  assert.equal(item(report, 'ram-usage').level, 'watch');
  assert.equal(report.items[0].id, 'disk-usage');
  assert.equal(report.items[0].level, 'high');
});

test('program evidence ranks measured values without changing the source order', () => {
  const groups = [{name: 'Small', cpu_pct: 2}, {name: 'Large', cpu_pct: 40}, {name: 'Middle', cpu_pct: 10}, {name: 'Missing', cpu_pct: null}];
  const before = structuredClone(groups);
  const report = buildResourceReport(sample({cpu_pct: 75}, groups));
  assert.match(item(report, 'cpu-usage').evidence, /Large 40.0%.*Middle 10.0%.*Small 2.0%/);
  assert.deepEqual(groups, before);
});

test('unknown process GPU coverage cannot attribute a busy GPU to a program', () => {
  const snapshot = sample({gpus: [{name: 'GPU A', util_pct: 100}]}, [{name: 'Old sample', gpu_pct: 99}]);
  snapshot.coverage = {process_gpu_available: false};
  const row = item(buildResourceReport(snapshot), 'gpu-usage');
  assert.match(row.evidence, /특정할 수 없습니다/);
  assert.doesNotMatch(row.evidence, /Old sample/);
});

test('GPU report separates busy-device identity from hottest sensor identity', () => {
  const snapshot = sample({gpus: [{name: 'Cool busy GPU', util_pct: 98, temp_c: 50}, {name: 'Hot idle GPU', util_pct: 0, temp_c: 92}]});
  const report = buildResourceReport(snapshot);
  assert.match(item(report, 'gpu-usage').note, /Cool busy GPU/);
  assert.match(item(report, 'gpu-temperature').evidence, /Hot idle GPU/);
  assert.doesNotMatch(item(report, 'gpu-temperature').evidence, /Cool busy GPU/);
  assert.equal(temperatureReadings(snapshot.system).sensors.gpu.temp_c, 92);
});

test('thermal thresholds are independent of utilization and do not invent a physical cause', () => {
  const report = buildResourceReport(sample({temperatures: {
    cpu: {temp_c: 85}, ram: {temp_c: 65}, disk: {temp_c: 55},
  }}));
  assert.equal(item(report, 'cpu-temperature').level, 'high');
  assert.equal(item(report, 'ram-temperature').level, 'watch');
  assert.equal(item(report, 'disk-temperature').level, 'watch');
  assert(!item(report, 'cpu-usage'));
  assert.match(item(report, 'cpu-temperature').note, /確定|확정할 수 없습니다/);
});

test('disk evidence describes logical I/O and never pins physical activity to C drive', () => {
  const report = buildResourceReport(sample({disk_busy_pct: 95, disk_read_mbps: 1, disk_write_mbps: 2}, [
    {name: 'Writer', io_read_mbps: 0, io_write_mbps: 30}, {name: 'Reader', io_read_mbps: 20, io_write_mbps: 0},
  ]));
  const row = item(report, 'disk-usage');
  assert.match(row.evidence, /논리 I\/O 상위: Writer 30.0 MiB\/s/);
  assert.match(row.note, /확정할 수 없습니다/);
  assert.doesNotMatch(row.note, /C:/);
});

test('RAM evidence reports capacity and aggregate limitations', () => {
  const row = item(buildResourceReport(sample({ram_pct: 90, ram_used_gb: 43.2, ram_total_gb: 48, ram_available_gb: 4.8},
    [{name: 'Synthetic editor', ram_gb: 8}])), 'ram-usage');
  assert.match(row.evidence, /8.0 GiB/);
  assert.match(row.note, /공유 메모리/);
  assert.match(row.note, /4.8 GiB/);
});

test('half of a gigabit link is not a warning or a slow-Internet diagnosis', () => {
  assert.deepEqual(buildResourceReport(sample({network: {kind: 'wired', status: 'ok', link_mbps: 1000, download_mbps: 500, upload_mbps: 400}})).items, []);
  const report = buildResourceReport(sample({network: {kind: 'wired', status: 'ok', link_mbps: 1000, download_mbps: 700, upload_mbps: 900}}));
  assert.equal(item(report, 'network-download_mbps').level, 'watch');
  assert.equal(item(report, 'network-upload_mbps').level, 'high');
  assert.match(item(report, 'network-upload_mbps').note, /고장 판정이 아닙니다/);
});

test('unknown link rate is not treated as congestion; explicit disconnect is high', () => {
  assert.deepEqual(buildResourceReport(sample({network: {status: 'ok', link_mbps: 0, download_mbps: 999}})).items, []);
  assert.equal(item(buildResourceReport(sample({network: {kind: 'disconnected'}})), 'network-disconnected').level, 'high');
});

test('stale readings suppress every prior warning including disconnects', () => {
  const snapshot = sample({cpu_pct: 100, network: {kind: 'disconnected'}, temperatures: {cpu: {temp_c: 95}}});
  snapshot.stale = true;
  assert.deepEqual(buildResourceReport(snapshot), {state: 'waiting', items: [], ts: null});
});

class Element {
  constructor() { this.children = []; this.dataset = {}; this.textContent = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
}
function documentFixture() {
  const elements = new Map(['resource-report', 'resource-report-status', 'resource-report-updated', 'resource-report-list'].map(id => [id, new Element()]));
  return {getElementById: id => elements.get(id), createElement: () => new Element()};
}
function textContent(node) { return [node.textContent, ...node.children.map(textContent)].join(' '); }

test('live report replaces values, removes recovered rows and clears on a connection error', () => {
  const doc = documentFixture();
  renderResourceReport(doc, sample({cpu_pct: 90, ram_pct: 81}));
  assert.equal(doc.getElementById('resource-report-list').children.length, 2);
  assert.equal(doc.getElementById('resource-report-status').textContent, '높음 1건 · 주의 1건');
  renderResourceReport(doc, sample({cpu_pct: 73}));
  assert.equal(doc.getElementById('resource-report-list').children.length, 1);
  assert.match(textContent(doc.getElementById('resource-report-list')), /73.0%/);
  assert.doesNotMatch(textContent(doc.getElementById('resource-report-list')), /90.0%/);
  renderResourceReport(doc, sample());
  assert.equal(doc.getElementById('resource-report-list').hidden, true);
  renderResourceReport(doc, null);
  assert.equal(doc.getElementById('resource-report').dataset.state, 'waiting');
  assert.deepEqual(doc.getElementById('resource-report-list').children, []);
  assert.doesNotMatch(doc.getElementById('resource-report-status').textContent, /상태가 없습니다/);
});

test('untrusted program names remain literal text, never HTML', () => {
  const doc = documentFixture();
  const name = '<img src=x onerror=alert(1)>';
  renderResourceReport(doc, sample({cpu_pct: 90}, [{name, cpu_pct: 60}]));
  assert.match(textContent(doc.getElementById('resource-report-list')), /<img src=x onerror=alert\(1\)>/);
  const walk = node => { assert(!Object.hasOwn(node, 'innerHTML')); node.children.forEach(walk); };
  walk(doc.getElementById('resource-report-list'));
});
