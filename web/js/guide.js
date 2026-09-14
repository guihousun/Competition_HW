/* Legend thumbnails reuse the map painters; no separate game representation. */
(function (global) {
  'use strict';
  const HW = global.HW;
  const units = [
    ['station','基地','核心建筑，关注血量'], ['worker','工人','采集、建造，也可操控炮台'],
    ['pioneer','开拓者','处理任务，也可操控炮台'], ['wall','围墙','阻挡移动；己方子弹可穿过'],
    ['gatling','加特林','近程多发弹道'], ['railgun','电磁炮','沿弹道消耗能量穿透'],
    ['rocket','火箭炮','范围爆炸，发射后有冷却'], ['smallRobot','小型机器人','来袭目标'],
    ['middleRobot','中型机器人','来袭目标'], ['largeRobot','大型机器人','高血量目标'], ['bossRobot','BOSS','高血量目标'],
  ];
  const sites = [
    ['stone','石矿','工人采集石头'],['iron','铁矿','工人采集铁矿石'],['copper','铜矿','工人采集铜矿石'],
    ['vendor','小贩','在旁边出售矿石'],['weaponShop','武器商店','在旁边购买物品'],
    ['challengerTaskPoint1','蓝方任务点','蓝方开拓者领取任务'],['defenderTaskPoint1','红方任务点','红方开拓者领取任务'],
  ];
  function catalog(id, entries, zone) {
    const box = document.getElementById(id);
    for (const [kind, name, note] of entries) {
      const card = document.createElement('div'); card.className = 'guide-card';
      const canvas = document.createElement('canvas'); canvas.width = 160; canvas.height = 140;
      canvas.setAttribute('aria-hidden', 'true');
      const ctx = canvas.getContext('2d'); ctx.scale(2, 2);
      if (zone) {
        HW.sprites.drawZone(ctx, 40, 42, 36, {kind, ...HW.ZONE_STYLE[kind], size:1, known:true});
      } else {
        const group = kind.endsWith('Robot') ? 'robot' : 'teamOur';
        const actor = HW.viewModel.actorsFromState({[group]:{roles:[{id:0,roleType:kind,pos:{x:0,y:0},health:100}]}})[0];
        HW.sprites.paint(ctx, actor, 40, 44, kind === 'station' ? 23 : 34);
      }
      const title = document.createElement('b'); title.textContent = name;
      const text = document.createElement('small'); text.textContent = note;
      card.append(canvas, title, text); box.append(card);
    }
  }
  function install(app) {
    catalog('guide-units', units, false); catalog('guide-sites', sites, true);
    const dialog = document.getElementById('map-guide');
    document.getElementById('open-map-guide').addEventListener('click', () => { app.stop(); dialog.showModal(); });
    document.getElementById('close-map-guide').addEventListener('click', () => dialog.close());
    dialog.addEventListener('click', (event) => { if (event.target === dialog) { const r = dialog.getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) dialog.close(); } });
  }
  function update(app, world) {
    const hint = document.getElementById('interaction-hint');
    if (!app || !app.world) return;
    hint.textContent = app.playing ? '正在自动推进 · 空格暂停后可逐轮查看'
      : world.mode === 'record' ? '录像回放 · 拖动时间轴回看，不重新模拟'
      : world.mode === 'sample' ? '单轮快照 · 打开调试面板运行策略'
      : '已暂停 · 前进一轮查看行动，点击角色卡放大定位';
  }
  HW.guide = {install, update};
}(window));
