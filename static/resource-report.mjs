// The cards and this report share thresholds and sensor selection.
import rules from './resource-rules.mjs';
const finite = value => typeof value === 'number' && Number.isFinite(value);
const rows = value => Array.isArray(value) ? value : [];
const number = (value, digits = 1) => finite(value) ? value.toLocaleString('ko-KR', {
  minimumFractionDigits: digits, maximumFractionDigits: digits,
}) : '—';
export const usageLimits = Object.freeze(rules.usage);
export const thermalLimits = Object.freeze(rules.thermal);
export const levelFor = (value, watch, high) => !finite(value) ? 'unknown' : value >= high ? 'high' : value >= watch ? 'watch' : 'safe';
export function systemDiskCapacity(system = {}) {
  const normalize = value => String(value || '').replace(/[\\/]+$/, '').toUpperCase();
  const drive = system.hardware?.system_disk?.drive;
  if (!drive) return null;
  const disk = rows(system.disks).find(item => normalize(item.drive) === normalize(drive));
  if (!disk || !finite(disk.total_gb) || !finite(disk.free_gb) || disk.total_gb <= 0 || disk.free_gb < 0 || disk.free_gb > disk.total_gb) return null;
  return {drive: normalize(drive), total: disk.total_gb, free: disk.free_gb, used: disk.total_gb - disk.free_gb};
}

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
