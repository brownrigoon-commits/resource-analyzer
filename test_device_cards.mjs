import test from 'node:test';
import assert from 'node:assert/strict';
import {deviceModels} from './static/device-cards.mjs';

test('two GPUs retain their own usage and temperature when the hottest/busiest differ',()=>{
  const system={gpus:[{id:'nvidia',name:'RTX',util_pct:99,temp_c:51},{id:'amd',name:'Radeon',util_pct:8,temp_c:42}],temperatures:{gpu:{sensors:[{name:'RTX · Hot Spot',temp_c:64}]}}};
  const cards=deviceModels(system);
  assert.deepEqual(cards.map(c=>[c.id,c.value,c.temp]),[['nvidia',99,51],['amd',8,42]]);
  assert.match(cards[0].sensorExtra,/64/);
  assert.equal(cards[1].sensorExtra,'');
  assert.ok(deviceModels(system,true).every(c=>c.value===null&&c.temp===null&&!c.sensorExtra));
});

test('same-model disks use physical drive number, not inventory order or model name',()=>{
  const cards=deviceModels({hardware:{storage:[{number:1,name:'same SSD',drives:['E:'],bus:'SATA'},{number:0,name:'same SSD',drives:['D:'],bus:'SATA'}]},
    physical_disk_io:{0:{read_mbps:2,write_mbps:3},1:{read_mbps:10,write_mbps:20}},physical_disk_activity:{0:5,1:90},
    temperatures:{disk:{sensors:[{hardware_id:'/ssd/0',temp_c:25},{hardware_id:'/ssd/1',temp_c:40}]}},
    disks:[{drive:'D:\\',total_gb:500,free_gb:300},{drive:'E:\\',total_gb:500,free_gb:100}]});
  assert.deepEqual(cards.map(c=>[c.id,c.value,c.temp,c.volumes[0].free]),[['disk-0',5,25,300],['disk-1',30,40,100]]);
});

test('missing disk telemetry remains missing and all mapped volumes stay separate',()=>{
  const [card]=deviceModels({hardware:{storage:[{number:4,name:'USB',bus:'USB',drives:['F:','G:']}]},disks:[{drive:'F:',total_gb:100,free_gb:10},{drive:'G:',total_gb:200,free_gb:20}]});
  assert.equal(card.value,null);assert.equal(card.temp,null);assert.match(card.label,/외장/);assert.equal(card.volumes.length,2);
});
