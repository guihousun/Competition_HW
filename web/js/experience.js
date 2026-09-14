/* Plain-language viewing layer. Derived from committed state; owns no game rules. */
(function (global) {
  'use strict';
  const HW = global.HW;
  const $ = (id) => document.getElementById(id);

  /* update() runs for every rendered frame. Writing a value that did not change
   * still dirties the node, so text and flags are only assigned when they differ. */
  function setText(node, value) {
    if (!node) return;
    const next = String(value);
    if (node.textContent !== next) node.textContent = next;
  }
  function setFlag(node, prop, value) {
    if (node && node[prop] !== value) node[prop] = value;
  }

  function brief(world) {
    if (!world || world.mode === 'idle') return { title: '准备出发', text: '开始后，策略会自动控制单位。你只需观察，也可以随时暂停。', countdown: '一场对局最多 10 天', progress: 0 };
    if (world.mode === 'sample') return { title: '官方单轮示例', text: '这是一张观测快照，不是一场比赛。打开调试面板，可以查看请求并运行一次策略。', countdown: '不包含连续对局状态', progress: 0 };
    if (world.result().done) return { title: '本地对局已结束', text: '可以回到开头重新观看，或通过时间轴定位关键事件。这里的结果不是官方成绩。', countdown: '所有已录制回合都可回看', progress: 1 };
    const phase = world.phase;
    const left = phase.total - phase.inPhase + 1;
    return { title: `第 ${phase.day} 天 · ${phase.isDay ? '白天建设' : '夜间防守'}`,
      text: phase.isDay ? '观察工人的采集与建造，以及开拓者的任务行动。入夜后，炮台需要角色在旁操控。'
        : `场上有 ${world.counts().robots} 个机器人。关注基地血量、炮台开火和角色是否到位。`,
      countdown: `距离${phase.isDay ? '入夜' : '天亮'}还有 ${left} 轮（含当前轮）`, progress: (phase.inPhase - 1) / phase.total };
  }

  function selection(actor) {
    setFlag($('selection-card'), 'hidden', !actor);
    if (!actor) return;
    setText($('selection-title'), `${actor.label} #${actor.id} · ${actor.owner === 'own' ? '我方' : actor.owner === 'enemy' ? '敌方' : '机器人'}`);
    setText($('selection-text'), `生命 ${actor.health} / ${actor.maxHealth}，位置 (${actor.pos.x}, ${actor.pos.y})` +
      (actor.capacity ? `。背包 ${actor.backpack.length} / ${actor.capacity}` : '') +
      (actor.cooldown ? `。还需冷却 ${actor.cooldown} 轮` : ''));
  }

  /* --------------------------------------------------------------- crew list */
  /* Keyed, stable buttons. Frames rebuild every actor object, so a handler that
   * closed over one would act on a position the match already left; the list is
   * reconciled in place instead of rebuilt, which also keeps focus and a click
   * that straddles a frame on the same node. */
  const crewEntries = new Map();
  let crewHost = null;
  let crewEmpty = null;

  function crewButton(key) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'crew-button';
    const name = document.createElement('b');
    const health = document.createElement('span');
    const detail = document.createElement('small');
    button.append(name, health, detail);
    button.addEventListener('click', () => {
      const app = HW.app;
      const actor = app && app.world && app.world.actors.find((a) => a.key === key);
      if (!actor) return;
      app.stop();
      app.selected = actor;
      app.renderer.selected = actor;
      app.fitMode = false;
      app.follow = false;
      app.renderer.camera.scale = Math.max(app.renderer.camera.scale, 0.8);
      app.renderer.centerOnActor(app.world, actor);
      app.panel.inspect({ type: 'actor', data: actor });
      selection(actor);
    });
    return { button, name, health, detail };
  }

  function syncCrew(container, actors, app) {
    if (!container) return;
    if (crewHost !== container) { crewEntries.clear(); crewHost = container; crewEmpty = null; }
    if (!actors.length) {
      for (const entry of crewEntries.values()) entry.button.remove();
      crewEntries.clear();
      const message = app && app.world ? '当前没有存活的行动角色。' : '1 名开拓者 · 2 名工人';
      if (crewEmpty !== message) { container.textContent = message; crewEmpty = message; }
      return;
    }
    if (crewEmpty !== null) { container.replaceChildren(); crewEmpty = null; }
    const preview = (app && app.previewCommands) || {};
    const seen = new Set();
    actors.forEach((actor, index) => {
      seen.add(actor.key);
      let entry = crewEntries.get(actor.key);
      if (!entry) {
        entry = crewButton(actor.key);
        crewEntries.set(actor.key, entry);
      }
      setText(entry.name, `${actor.label} #${actor.id}`);
      setText(entry.health, `生命 ${actor.health}/${actor.maxHealth}`);
      const command = preview[String(actor.id)];
      const controlsTower = Object.values(preview).some((c) => String(c.controllerId) === String(actor.id));
      setText(entry.detail, `背包 ${actor.backpack.length}/${actor.capacity || '—'} · ` + (controlsTower ? '下一步：操控炮台开火'
        : command ? `下一步：${HW.util.actionName(command.action)}` : '当前未分配指令'));
      const label = `定位${actor.label} ${actor.id}`;
      if (entry.button.getAttribute('aria-label') !== label) entry.button.setAttribute('aria-label', label);
      const at = container.children[index];
      if (at !== entry.button) container.insertBefore(entry.button, at || null);
    });
    for (const [key, entry] of Array.from(crewEntries)) {
      if (seen.has(key)) continue;
      entry.button.remove();
      crewEntries.delete(key);
    }
  }

  /* ------------------------------------------------------------- brief events */
  const briefEntries = [];
  let briefHost = null;
  let briefEmpty = null;
  const EVENT_PRIORITY = { skip: 0, death: 1, task: 2, attack: 3, build: 4 };

  function syncBrief(container, lines, fallback) {
    if (!container) return;
    if (briefHost !== container) { briefEntries.length = 0; briefHost = container; briefEmpty = null; }
    const shown = lines.slice()
      .sort((a, b) => (EVENT_PRIORITY[a.type] ?? 9) - (EVENT_PRIORITY[b.type] ?? 9))
      .slice(0, 3);
    if (!shown.length) {
      for (const node of briefEntries) node.remove();
      briefEntries.length = 0;
      if (briefEmpty !== fallback) { container.textContent = fallback; briefEmpty = fallback; }
      return;
    }
    if (briefEmpty !== null) { container.replaceChildren(); briefEmpty = null; }
    while (briefEntries.length > shown.length) briefEntries.pop().remove();
    shown.forEach((line, index) => {
      let node = briefEntries[index];
      if (!node) { node = document.createElement('p'); container.append(node); briefEntries[index] = node; }
      setText(node, line.text);
      const className = `brief-event ${line.type}`;
      if (node.className !== className) node.className = className;
    });
  }

  function update(world) {
    const app = HW.app;
    const ready = Boolean(app && app.world);
    const busy = Boolean(app && app.busy);
    const llm = ready && world.state && world.state._demo || {};
    const llmStatus = llm.llm_status || {};
    const labels = {running: '正在请求，游戏回合暂停等待', done: '回答已返回', failed: '调用失败', disabled: '未启用真实 API'};
    setText($('llm-channel-status'), llm.llm_enabled
      ? 'DeepSeek · deepseek-flash / max · ' + (labels[llmStatus.status] || '已启用，等待任务请求') + (llmStatus.error ? '（' + llmStatus.error + '）' : '')
      : '普通场景不调用真实 API。可在对局设置中体验 LLM 任务。');
    setFlag($('llm-demo'), 'disabled', busy);
    if (HW.guide) HW.guide.update(app, world);
    const root = $('app');
    if (root && root.dataset.ready !== String(ready)) root.dataset.ready = String(ready);
    const message = brief(ready ? world : null);
    setText($('brief-title'), message.title);
    setText($('brief-text'), message.text);
    setText($('brief-countdown'), message.countdown);
    const fill = $('phase-meter-fill');
    const fillWidth = `${message.progress * 100}%`;
    if (fill && fill.style.width !== fillWidth) fill.style.width = fillWidth;
    const phase = ready && !world.phase.isDay ? 'night' : 'day';
    if (root && root.dataset.phase !== phase) root.dataset.phase = phase;
    setFlag($('map-caption'), 'hidden', !ready);
    const playable = ready && world.mode !== 'sample';
    setText($('play'), app && app.playing ? '⏸ 暂停' : '▶ 继续');
    setFlag($('play'), 'disabled', !(app && app.playing) && (!playable || busy || (world.done && world.index === world.maxIndex)));
    setFlag($('step'), 'disabled', !playable || busy || (world.done && world.index === world.maxIndex));
    setFlag($('stepback'), 'disabled', !ready || busy || world.index === 0);
    setFlag($('reset'), 'disabled', !ready || busy);
    setFlag($('save-replay'), 'disabled', !ready || busy);
    setText($('newmatch'), ready ? '开始新对局' : '开始模拟');
    syncCrew($('crew-list'), ready ? world.actors.filter((a) => a.owner === 'own' && ['worker', 'pioneer'].includes(a.kind)) : [], app);
    const task = HW.viewModel.taskStatus(ready ? world : null);
    setText($('task-status-title'), task.title);
    setText($('task-status-description'), task.description);
    setText($('task-status-time'), task.meter);
    setText($('task-status-hint'), task.hint);
    const taskMeter = $('task-status-meter');
    if (taskMeter) {
      setFlag(taskMeter, 'hidden', task.ratio == null);
      if (task.ratio != null) setFlag(taskMeter, 'value', task.ratio);
      if (taskMeter.getAttribute('aria-label') !== task.meter) taskMeter.setAttribute('aria-label', task.meter);
    }
    syncBrief($('brief-events'), ready ? world.describe(world.index) : [],
      ready && world.index ? '这一轮没有记录到行动。' : '还没有已执行的行动。');
    selection(ready ? app.selected : null);
  }
  HW.experience = {brief, update, selection};
}(window));
