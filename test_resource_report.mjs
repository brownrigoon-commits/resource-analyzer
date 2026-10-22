import test from 'node:test';
import assert from 'node:assert/strict';
import {usageLimits, thermalLimits, levelFor, temperatureReadings, systemDiskCapacity} from './static/resource-report.mjs';

test('shared thresholds include exact warning and high boundaries', () => {
  assert.equal(levelFor(69.9, ...usageLimits.cpu), 'safe');
  assert.equal(levelFor(70, ...usageLimits.cpu), 'watch');
  assert.equal(levelFor(90, ...usageLimits.cpu), 'high');
  assert.equal(levelFor(80, ...usageLimits.ram), 'watch');
  assert.equal(levelFor(null, ...usageLimits.cpu), 'unknown');
  assert.equal(levelFor(85, ...thermalLimits.cpu), 'high');
});
test('hottest GPU sensor is independent of busiest GPU', () => {
  const result = temperatureReadings({gpus:[{name:'Busy cool',util_pct:99,temp_c:40},{name:'Idle hot',util_pct:0,temp_c:95}]});
  assert.equal(result.sensors.gpu.temp_c,95);
  assert.match(result.sensors.gpu.details,/Idle hot/);
});
test('system disk capacity uses the actual drive mapping rather than the first disk', () => {
  const value = systemDiskCapacity({hardware:{system_disk:{drive:'C:'}},disks:[
    {drive:'E:\\',total_gb:500,free_gb:20},{drive:'c:\\',total_gb:2000,free_gb:1500},
  ]});
  assert.deepEqual(value,{drive:'C:',total:2000,free:1500,used:500});
});
test('missing or inconsistent capacity is not reported as zero free space', () => {
  assert.equal(systemDiskCapacity({}),null);
  assert.equal(systemDiskCapacity({hardware:{system_disk:{drive:'C:'}},disks:[{drive:'D:',total_gb:2000,free_gb:1500}]}),null);
  assert.equal(systemDiskCapacity({hardware:{system_disk:{drive:'C:'}},disks:[{drive:'C:',total_gb:2000,free_gb:2500}]}),null);
});
