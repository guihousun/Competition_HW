/* Run with node tools/check_combat_visuals.cjs. No browser or paid API needed. */
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const box = {window: {}, performance: {now: () => 1000}};
vm.createContext(box);
for (const name of ['constants', 'sprites', 'viewmodel', 'effects', 'renderer']) {
  vm.runInContext(fs.readFileSync(path.join(root, 'web/js', name + '.js'), 'utf8'), box);
}
const HW = box.window.HW;
function ctx() {
  const calls = [];
  return new Proxy({calls}, {
    get(t, k) {
      if (k in t) return t[k];
      if (k === 'createRadialGradient') return () => ({addColorStop() {}});
      return (...args) => { calls.push([k, ...args]); };
    },
    set(t, k, v) { calls.push(['set', k, v]); t[k] = v; return true; },
  });
}
const state = level => ({roundNo: 90, mapInfo:{width:41,height:32,zones:[]},
  teamOur:{type:'challenger',roles:[{id:1,roleType:'rocket',pos:{x:9,y:20},level,health:1000}]},
  teamEnemy:{roles:[]},robot:{roles:[]}});
const world = HW.World.fromScenario({state:state(1)});
world.pushStep({state:state(3),frame:{actions:[]}});
assert.equal(world.actors[0].level,3);
const effects = new HW.Effects();
effects.spawnForFrame(world,{actions:[]},600,{height:32,tile:44});
assert.equal(effects.items.filter(e=>e.text && e.text.includes('1 → 3')).length,1);
world.applyFrame(0,{instant:true});effects.clear();
effects.spawnForFrame(world,{actions:[]},600,{height:32,tile:44});
assert.equal(effects.items.length,0,'rewind is not an upgrade');

const from={x:1,y:1},to={x:5,y:5};
function shot(kind,hits=[]) {return {a:'attack',kind,from,level:3,
  salvos:[{cell:to,path:[from,to],hits}]};}
effects.spawnForFrame(world,{actions:[shot('rocket',[{robot:20,damage:20,cell:to}]),shot('railgun')]},600,{height:32,tile:44});
const rocket=effects.items.find(e=>e.type==='rocket');
const beam=effects.items.find(e=>e.type==='beam');
const blast=effects.items.find(e=>e.type==='explosion');
assert.ok(rocket && beam && blast);
assert.equal(rocket.path,null,'missiles are not straight ballistic paths');
assert.equal(blast.born,rocket.born+rocket.ttl,'explosion follows arrival');
assert.ok(effects.items.find(e=>e.type==='damage').born >= blast.born);
assert.equal(effects.items.filter(e=>e.type==='damage').length,1);
const c=ctx();effects.draw(c,rocket.born+rocket.ttl/2);
assert.ok(c.calls.some(row=>row[0]==='rotate'),'missile has oriented body');

const fakeRenderer={camera:{scale:0.4}};
const overlay=ctx();
HW.Renderer.prototype.drawCommands.call(fakeRenderer,overlay,world,
  {'1':{action:'attack',from,targetPos:[to]}},44,{height:32},'#fff',false);
assert.ok(!overlay.calls.some(row=>row[0]==='lineTo'),'attack command is not a fake laser');
for(const kind of ['rocket','railgun','wall']) {
  const pictures=[];
  for(const level of [1,2,3]) {
    const actor={...world.actors[0],kind,level,color:HW.OWNER_COLORS.own};
    const paint=ctx();HW.sprites.paint(paint,actor,100,100,44);
    HW.Renderer.prototype.drawLevel.call(fakeRenderer,paint,actor,100,100,44);
    assert.ok(paint.calls.some(row=>row[0]==='fillText' && row[1]===String(level)));
    pictures.push(JSON.stringify(paint.calls));
  }
  assert.equal(new Set(pictures).size,3,kind+' must have three visible tiers');
}
for(let i=0;i<500;i++) effects.spawnForFrame(world,{actions:[shot('rocket')]},75,{height:32,tile:44});
assert.ok(effects.count<=HW.MAX_EFFECTS);
effects.update(1e9);assert.equal(effects.count,0);
console.log('PASS: real levels, rewind, distinct shot types, impact timing, no fake laser, 3 visual tiers, bounded effects.');
