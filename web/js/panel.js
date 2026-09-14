/* Panel: every DOM update outside the canvas (HUD, dev tabs, logs, rules).
 *
 * One rule matters most here: preview commands and executed results are never
 * merged into the same list, so a user can always tell "what the policy wants"
 * apart from "what the local simulator actually did".
 */
(function (global) {
  'use strict';

  const HW = global.HW = global.HW || {};
  const U = HW.util;

  const EVENT_TYPES = [
    { id: 'move', label: '移动' },
    { id: 'collect', label: '采集' },
    { id: 'build', label: '建造' },
    { id: 'attack', label: '开火' },
    { id: 'robot', label: '机器人' },
    { id: 'kill', label: '击毁' },
    { id: 'death', label: '阵亡' },
    { id: 'spawn', label: '波次' },
    { id: 'gold', label: '金币' },
    { id: 'task', label: '任务' },
    { id: 'skip', label: '失败指令' },
  ];
  const EVENT_CLASS = {
    move: 'ok', collect: 'ok', build: 'ok', attack: 'ok', robot: '', kill: 'ok',
    death: 'fail', spawn: '', gold: '', task: 'ok', skip: 'fail',
  };

  function $(id) { return document.getElementById(id); }
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /* The HUD, timeline and panels are rewritten on every rendered frame. Writing
   * a value that did not change still dirties the node, so these helpers keep
   * the DOM (and the layout) quiet while the match plays. */
  function setText(node, value) {
    if (!node) return;
    const next = String(value);
    if (node.textContent !== next) node.textContent = next;
  }
  function setValue(node, value) {
    if (!node) return;
    const next = String(value);
    if (node.value !== next) node.value = next;
  }
  function setDisabled(node, disabled) {
    if (node && node.disabled !== disabled) node.disabled = disabled;
  }

  class Panel {
    constructor() {
      this.hiddenTypes = new Set();
      this.eventFilterBuilt = false;
      this.autoScroll = true;
      this.window = 24;
      // Blocking work (scene, recording, import) locks the transport; a routine
      // /debug/step only sets stepPending, which never disables anything.
      this.busy = false;
      this.stepPending = false;
      // The request box is editable: remember when the user changed it so frame
      // updates stop overwriting work that has not been applied yet.
      this.requestDirty = false;
      this._twoMatchRendered = undefined;
    }

    /* ----------------------------------------------------------- HUD */
    updateHud(world, status) {
      const phase = world.phase;
      const station = world.stationHealth();
      const counts = world.counts();
      setText($('hud-round'), `第 ${world.roundNo} 回合`);
      setText($('hud-round-sub'), world.index === 0
        ? '初始布局（未结算）'
        : `已记录 ${world.frameCount} 回合 · 显示第 ${world.index} 帧`);
      setText($('hud-phase'), `${phase.label} D${phase.day}`);
      setText($('hud-phase-sub'), `${phase.phaseLabel} ${phase.inPhase}/${phase.total}`);
      setText($('hud-gold'), String(world.gold));
      setText($('hud-gold-sub'), `武器 ${HW.OFFICIAL.weaponCost} 金币 / 上限 ${HW.OFFICIAL.towerLimit} 座`);
      const ratio = U.clamp(station.total / (station.max || 1), 0, 1);
      setText($('hud-base'), station.dead ? '已摧毁' : `${station.total} / ${station.max}`);
      const bar = $('hud-base-bar');
      const width = `${Math.round(ratio * 100)}%`;
      if (bar.style.width !== width) bar.style.width = width;
      const barClass = ratio > 0.55 ? '' : (ratio > 0.25 ? 'warn' : 'bad');
      if (bar.className !== barClass) bar.className = barClass;
      setText($('hud-score'), String(world.score));
      setText($('hud-kills'), String(world.kills));
      setText($('hud-units'), `${counts.own}`);
      setText($('hud-units-sub'), `工人 ${counts.workers} · 开拓者 ${counts.pioneers} · 炮台 ${counts.towers} · 墙 ${counts.walls}`);
      setText($('hud-robots'), String(counts.robots));
      setText($('hud-robots-sub'), counts.robots ? '目标优先：阻挡移动者' : '当前无机器人');
      setText($('hud-status'), status.label);
      setText($('hud-status-sub'), status.detail);
      if (HW.experience) HW.experience.update(world);
    }

    /**
     * Timeline readout. `options.preserveScrub` keeps the thumb of a drag that is
     * still in progress: a frame or a pending step landing mid-drag must not
     * fight the user's input.
     */
    updateTimeline(world, playing, options) {
      const opts = options || {};
      const scrub = $('scrub');
      const max = String(world.maxIndex);
      if (scrub.max !== max) scrub.max = max;
      if (!opts.preserveScrub) setValue(scrub, world.index);
      setDisabled(scrub, this.busy || world.maxIndex === 0);
      setText($('timeline-label'), world.index === 0 ? `起始状态 · 已记录 ${world.maxIndex} 轮`
        : `已推进 ${world.index} / ${world.maxIndex} 轮 · 当前回合 ${world.roundNo}`);
      setText($('timeline-hint'), world.mode === 'record'
        ? '回放：可任意回看，不会重新模拟'
        : (playing ? '策略正在自动行动，可以随时暂停' : '拖动回看；前进一轮查看下一步'));
    }

    /** The frame number field, left alone while the user is still typing in it. */
    syncSeekFrame(world, preserve) {
      const node = $('seek-frame');
      if (!node) return;
      const max = String(world.maxIndex);
      if (node.max !== max) node.max = max;
      if (!preserve) setValue(node, world.index);
    }

    setMode(mode, extra) {
      const badge = $('mode-badge');
      const map = {
        idle: ['未开始', ''],
        live: ['实时模拟', 'live'],
        record: ['回放记录', 'record'],
        done: ['对局结束', 'done'],
        sample: ['官方示例', 'live'],
      };
      const [label, cls] = map[mode] || map.idle;
      setText(badge, extra ? `${label} · ${extra}` : label);
      const className = `mode-badge ${cls}`;
      if (badge.className !== className) badge.className = className;
    }

    status(label) {
      setText($('hud-status'), label);
    }

    toast(message, kind) {
      const node = $('toast');
      if (!message) { node.hidden = true; return; }
      node.textContent = message;
      node.className = `toast${kind ? ` ${kind}` : ''}`;
      node.hidden = false;
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => { node.hidden = true; }, kind === 'error' ? 8000 : 3600);
    }

    banner(html, kind) {
      const node = $('banner');
      if (!html) { node.hidden = true; return; }
      node.innerHTML = html;
      node.className = `banner${kind ? ` ${kind}` : ''}`;
      node.hidden = false;
    }

    empty(show, reason) {
      $('empty-state').hidden = !show;
      if (show && reason) {
        $('empty-state').querySelector('p').textContent = reason;
      }
    }

    /* --------------------------------------------------- command lists */
    previewList(world, commands) {
      const container = $('plan-list');
      container.replaceChildren();
      const entries = Object.entries(commands || {});
      $('plan-note').textContent = entries.length
        ? `来自当前状态的角色指令（下一轮将要提交的指令，尚未结算）。`
        : '当前状态没有可提交的指令（白天采集/建造或夜晚无法开火时都可能为空）。';
      if (!entries.length) {
        container.append(el('div', 'empty-line', '本回合没有预览指令。'));
        return;
      }
      const byId = new Map(world.actors.map((a) => [String(a.id), a]));
      for (const [id, command] of entries) {
        const actor = byId.get(String(id));
        const item = el('div', 'item');
        item.append(el('span', 'idx', id));
        item.append(el('span', 'who', actor ? actor.label : '未知单位'));
        const parts = [U.actionName(command.action)];
        if (command.name) parts.push(U.itemName(command.name) === command.name
          ? U.kindName(command.name) : U.itemName(command.name));
        const targets = (command.targetPos || []).map((p) => U.cellLabel(p));
        if (targets.length) parts.push(targets.join(' → '));
        if (command.num) parts.push(`数量 ${command.num}`);
        if (command.controllerId) parts.push(`操控者 ${command.controllerId}`);
        if (command.taskAnswer) parts.push(`答案 ${String(command.taskAnswer).slice(0, 40)}`);
        if (command.item) parts.push(`献祭 ${command.item.map((i) => U.itemName(i)).join('、')}`);
        item.append(el('span', '', parts.join(' · ')));
        container.append(item);
      }
    }

    executedList(world, frame) {
      const container = $('exec-list');
      container.replaceChildren();
      if (!frame) {
        container.append(el('div', 'empty-line', '第 0 帧是初始布局，没有已执行的动作。'));
        return;
      }
      const executed = frame.executed || {};
      const results = (world.states[world.index] && world.states[world.index].lastRoundRoleActionResults) || {};
      const ids = Object.keys(executed);
      if (!ids.length) {
        container.append(el('div', 'empty-line', `第 ${frame.round} 回合策略没有提交任何指令。`));
      }
      for (const id of ids) {
        const command = executed[id] || {};
        const ok = results[id];
        const item = el('div', `item ${ok === false ? 'fail' : (ok === true ? 'ok' : '')}`);
        item.append(el('span', 'idx', id));
        item.append(el('span', 'who', `${U.actionName(command.action)}${command.name ? ` ${U.itemName(command.name)}` : ''}`));
        const detail = [];
        detail.push((command.targetPos || []).map((p) => U.cellLabel(p)).join(' / ') || '无目标');
        if (command.num) detail.push(`数量 ${command.num}`);
        if (command.controllerId) detail.push(`操控者 ${command.controllerId}`);
        if (command.taskAnswer) detail.push(`答案 ${String(command.taskAnswer).slice(0, 40)}`);
        detail.push(ok === false ? '结果：未执行' : (ok === true ? '结果：已执行' : '结果：未返回'));
        item.append(el('span', '', detail.join(' · ')));
        container.append(item);
      }
      const skipped = frame.skipped || [];
      if (skipped.length) {
        const note = el('div', 'item fail');
        note.append(el('span', 'idx', '失败'));
        note.append(el('span', 'who', '本地校验'));
        note.append(el('span', '', `${skipped.length} 条指令未执行（碰撞、目标被占用或规则校验失败）。这不等于协议异常。`));
        container.append(note);
      }
      const deaths = frame.robotDeaths || [];
      if (deaths.length) {
        const note = el('div', 'item ok');
        note.append(el('span', 'idx', '奖励'));
        note.append(el('span', 'who', '击杀分'));
        note.append(el('span', '', deaths.map((d) => `${U.kindName(d.kind)} +${d.score}`).join(' · ')));
        container.append(note);
      }
    }

    /* ------------------------------------------------------- event log */
    buildFilters() {
      if (this.eventFilterBuilt) return;
      const box = $('event-filters');
      box.replaceChildren();
      for (const type of EVENT_TYPES) {
        const button = el('button', '', type.label);
        button.type = 'button';
        button.dataset.type = type.id;
        button.addEventListener('click', () => {
          if (this.hiddenTypes.has(type.id)) this.hiddenTypes.delete(type.id);
          else this.hiddenTypes.add(type.id);
          button.classList.toggle('off', this.hiddenTypes.has(type.id));
          this.renderEvents(this._lastWorld, true);
        });
        box.append(button);
      }
      this.eventFilterBuilt = true;
    }

    /** Render the event log for the rounds up to world.index (windowed). */
    renderEvents(world, keepScroll) {
      this._lastWorld = world;
      this.buildFilters();
      const container = $('event-list');
      const previousScroll = container.scrollTop;
      container.replaceChildren();
      if (!world || world.index === 0) {
        container.append(el('div', 'empty-line', '还没有已结算的回合。'));
        return;
      }
      const start = Math.max(1, world.index - this.window + 1);
      let shown = 0;
      for (let index = start; index <= world.index; index += 1) {
        let lines = world.describe(index);
        if (this.hiddenTypes.size) lines = lines.filter((line) => !this.hiddenTypes.has(line.type));
        if (!lines.length) continue;
        container.append(el('div', 'log-round', `第 ${index} 回合（回合号 ${(world.states[index] && world.states[index].roundNo) || '?'}）`));
        for (const line of lines) {
          const item = el('div', `item ${EVENT_CLASS[line.type] || ''}`);
          item.append(el('span', 'idx', line.type));
          item.append(el('span', '', line.text));
          container.append(item);
          shown += 1;
        }
      }
      if (!shown) container.append(el('div', 'empty-line', '当前过滤条件下没有事件。'));
      if (keepScroll && this.autoScroll) container.scrollTop = container.scrollHeight;
      else container.scrollTop = previousScroll;
    }

    /* --------------------------------------------------------- inspect */
    inspect(target) {
      const body = $('inspect-body');
      body.replaceChildren();
      if (!target) {
        body.append(el('div', 'empty-line', '点击画面中的单位或中立点。'));
        return;
      }
      if (target.type === 'zone') {
        const zone = target.data;
        this._kv(body, '类型', `${zone.label}（${zone.kind}）`);
        this._kv(body, '坐标', U.cellLabel(zone.pos));
        this._kv(body, '占格', zone.size === 2 ? '2 格（任务点二）' : '1 格');
        this._kv(body, '数据来源', 'mapInfo.zones（官方观测字段）');
        if (!zone.known) {
          this._kv(body, '提示', '该中立类型未在本地美术表中建模，已按未知类型降级显示。');
        }
        return;
      }
      const actor = target.data;
      const ratio = U.clamp(actor.health / (actor.maxHealth || 1), 0, 1);
      this._kv(body, '单位', `${actor.label}（${actor.kind}）`);
      this._kv(body, '归属', actor.owner === 'own' ? '我方' : (actor.owner === 'enemy' ? '敌方（可见单位）' : '机器人（第三方）'));
      this._kv(body, '角色 ID', String(actor.id));
      this._kv(body, '坐标', `${U.cellLabel(actor.pos)} · 占格 ${actor.size}`);
      this._kv(body, '血量', `${actor.health} / ${actor.maxHealth}（${(ratio * 100).toFixed(0)}%）`);
      if (HW.OFFICIAL.towerTypes.includes(actor.kind)) {
        this._kv(body, '等级 / 射程', `Lv${actor.level} · 切比雪夫 ${HW.rangeOf(actor.kind, actor.level)}`);
        this._kv(body, '冷却', actor.cooldown > 0 ? `${actor.cooldown} 回合（火箭发射后 3 回合）` : '就绪');
      }
      if (actor.kind === 'station') {
        this._kv(body, '建筑血量档', HW.OFFICIAL.healthByLevel.station.join(' / '));
      }
      if (actor.capacity) {
        this._kv(body, '背包', `${actor.backpack.length}/${actor.capacity} ${actor.backpack.length ? `[${actor.backpack.join(', ')}]` : ''}`);
      }
      if (actor.owner === 'robot') {
        this._kv(body, '攻击力 / 击杀分', `${HW.OFFICIAL.robotAttack[actor.kind] || '—'} / ${HW.OFFICIAL.robotScore[actor.kind] || '—'}`);
        this._kv(body, '状态', actor.abnormalState || '正常');
      }
      if (actor.unmodelled) {
        this._kv(body, '提示', '未建模单位类型：使用降级图元显示，游戏逻辑未改动。');
      }
    }

    _kv(parent, key, value) {
      const row = el('div', 'kv');
      row.append(el('span', '', key));
      row.append(el('span', '', value));
      parent.append(row);
    }

    /* ------------------------------------------------------------ io */
    /**
     * Programmatic request text. Skipped while the user has unapplied edits,
     * unless the caller forces it (new scene, imported file, official sample).
     */
    setRequest(text, force) {
      const node = $('input-json');
      if (!node) return;
      if (this.requestDirty && !force) return;
      setValue(node, text);
    }

    markRequestEdited() { this.requestDirty = true; }
    acceptRequestEdit() { this.requestDirty = false; }

    setResponse(text, note) {
      const node = $('output-json');
      if (node && node.textContent !== text) node.textContent = text;
      if (note) setText($('resp-note'), note);
    }

    /* ------------------------------------------------------------ tasks */
    /**
     * Task panel: the official fields the judge publishes, plus our own record
     * of the cycle (answers submitted, rewards) marked clearly as local.
     */
    renderTasks(world, info) {
      const body = $('tasks-body');
      body.replaceChildren();
      if (!world || !world.state) {
        body.append(el('div', 'empty-line', '尚未创建对局。'));
        return;
      }
      const state = world.state;
      const phase = String(state.phaseTask || '').trim();
      const points = (state.teamOur && state.teamOur.playerTasks) || [];
      this._kv(body, 'phaseTask（官方字段）', phase ? `${phase.split('\n')[0]}（${phase.length} 字符）` : '当前没有已领取任务的描述');
      if (phase) {
        const pre = el('pre', 'code');
        pre.textContent = phase;
        pre.style.maxHeight = '120px';
        body.append(pre);
      }
      if (!points.length) {
        this._kv(body, '任务点', '本观测没有 playerTasks 字段（示例快照或未生成场景）');
      }
      for (const point of points) {
        const row = el('div', 'kv');
        row.append(el('span', '', `任务点 ${U.cellLabel(point.taskPosition)}`));
        row.append(el('span', '', [
          point.taskType || '未知类型',
          point.isValid ? '可领取' : '不可领取',
          `冷却 ${point.coldDownRounds ?? 0} 回合`,
          `超时 ${point.timeoutRounds ?? '—'} 回合`,
          `奖励 ${point.scoreReward ?? '—'} 分 / ${point.goldReward ?? '—'} 金币`,
        ].join(' · ')));
        body.append(row);
      }
      const local = info && info.taskReport;
      const rewards = (info && info.rewards) || [];
      const card = el('div', `diag-card${rewards.length ? '' : ' warn'}`);
      card.append(el('b', '', '本地任务夹具（离线演示，非官方）'));
      const lines = el('div');
      lines.style.marginTop = '4px';
      if (local && (local.accepted || local.ended || local.rewards)) {
        if (local.accepted) lines.append(el('div', '', `本回合接取：${local.accepted.split('\n')[0]}`));
        if (local.ended) lines.append(el('div', '', `本回合结束：${local.ended}`));
        if (local.rewards) {
          lines.append(el('div', '', `结算：通过率 ${(local.rewards.rate * 100).toFixed(0)}% · 积分 +${local.rewards.score} · 金币 +${local.rewards.gold}`));
          lines.append(el('div', '', `提交答案：${local.rewards.answer}`));
          lines.append(el('div', '', `参考答案：${local.rewards.expected}`));
        }
      } else {
        lines.append(el('div', '', '本帧没有任务结算记录。'));
      }
      if (rewards.length) {
        lines.append(el('div', '', `整场累计 ${rewards.length} 次任务奖励：` + rewards
          .map((r) => `第 ${r.round} 回合 ${(r.rate * 100).toFixed(0)}%/+${r.score}`).join('、')));
      }
      card.append(lines);
      body.append(card);

      const judge = (info && info.judge) || {};
      const channel = el('div', 'diag-card');
      channel.append(el('b', '', '判题器通道（prompt / executeCmd）'));
      const rows = el('div');
      rows.style.marginTop = '4px';
      for (const [label, value] of [
        ['本日 LLM 已用', `${judge.llmUsed ?? 0} / 3（任务期间不限额）`],
        ['待回 prompt', judge.pendingPrompt ? judge.pendingPrompt.slice(0, 60) : '无'],
        ['待回 executeCmd', judge.pendingCommand ? judge.pendingCommand.slice(0, 60) : '无'],
        ['lastCmdResult', judge.lastResult || '尚未执行沙盒命令'],
        ['本回合 llmResp', judge.llmResp ? judge.llmResp.slice(0, 60) : `本回合无返回（已消费 ${judge.llmResponses || 0} 次模型回答）`],
      ]) {
        rows.append(el('div', '', `${label}：${value}`));
      }
      channel.append(rows);
      body.append(channel);

      this._renderTreasure(body, state, info);
    }

    /**
     * Treasure state, read from the published rumour plus the local fixture's
     * record. The rumour is the only channel the rules provide (§4.8), so the site
     * and items are shown exactly as published; the reward values are local.
     */
    _renderTreasure(body, state, info) {
      const rumour = ((state.worldNews || {}).folkLegends) || '';
      const record = (state._demo || {}).treasure;
      if (!rumour && !record) return;
      const card = el('div', 'diag-card');
      card.append(el('b', '', '宝藏（本地夹具：原文只给出民间传闻）'));
      const lines = el('div');
      lines.style.marginTop = '4px';
      if (rumour) lines.append(el('div', '', rumour));
      if (record) {
        const round = state.roundNo || 0;
        const window = `${record.opensAt}-${record.closesAt} 回合`;
        const open = round >= record.opensAt && round <= record.closesAt;
        lines.append(el('div', '', `祭坛 ${U.cellLabel(record.site)} · 需献祭 ${
          (record.items || []).map((item) => U.itemName(item)).join('、')} · 窗口 ${window}${
          open ? '（已开启）' : ''}`));
        lines.append(el('div', '', record.opened
          ? `已被开启：${(record.openedBy || []).join('、')}（本地奖励 +${record.score} 分 / +${record.gold} 金币）`
          : '尚未开启（一张地图只有一个宝藏，同一回合双方都满足条件时都可获得）'));
      }
      card.append(lines);
      body.append(card);
    }

    /* ------------------------------------------------------- two-team */
    /**
     * Local 1v1 job: both teams, their scores and base health, and how far the
     * match has run. Clearly labelled local — this comparison is not an official
     * result and the settlement order differs from the judge's.
     */
    renderTwoMatch(info) {
      const body = $('twomatch-body');
      if (!body) return;
      // This is called once per rendered frame with the same job snapshot; only
      // a new snapshot (or the first render) needs to touch the DOM.
      if (info === this._twoMatchRendered) return;
      this._twoMatchRendered = info;
      body.replaceChildren();
      const data = info || {};
      if (!data.state || data.state === 'idle') {
        body.append(el('div', 'empty-line', data.note || '尚未开始本地双队对局。'));
        return;
      }
      this._kv(body, '状态', {
        running: `进行中（第 ${data.round} / ${data.maxRounds} 回合，已用 ${data.elapsed}s）`,
        done: `已结束（${data.round} 回合，用时 ${data.elapsed}s）`,
        failed: `失败：${data.error || '未知错误'}`,
      }[data.state] || data.state);
      this._kv(body, '种子 / 压力', `${data.seed} / ${data.pressure}`);

      const bar = el('div', 'bar');
      const fill = el('i');
      fill.style.width = `${Math.round((data.progress || 0) * 100)}%`;
      if (data.state === 'done') fill.className = 'warn';
      bar.append(fill);
      body.append(bar);

      for (const side of data.sides || []) {
        const row = el('div', 'kv');
        const label = side === (data.sides || [])[0] ? '先手方' : '后手方';
        row.append(el('span', '', `${label} ${side}`));
        row.append(el('span', '',
          `积分 ${(data.scores || {})[side] ?? '—'} · 基地 ${(data.baseHp || {})[side] ?? '—'}`));
        body.append(row);
      }
      if (data.result) {
        const result = data.result;
        const card = el('div', 'diag-card');
        card.append(el('b', '', '本地结果'));
        card.append(el('div', '', `胜者：${result.winner || '平局'} · ${result.reason}`));
        card.append(el('div', '',
          `比分 ${result.scores.challenger} : ${result.scores.defender}`));
        card.append(el('div', '', '结算顺序与异常上限未复现，不能当作官方成绩。'));
        body.append(card);
      }
      if ((data.lastEvents || []).length) {
        const pre = el('pre', 'code');
        pre.textContent = data.lastEvents.join('\n');
        body.append(pre);
      }
    }

    /* --------------------------------------------------------- rules */
    renderRules(payload) {
      const body = $('rules-body');
      body.replaceChildren();
      const labels = {
        official: '官方明确',
        approx: '非合规近似',
        local: '本地假设',
        gap: '实现缺口',
      };
      const table = el('table');
      const head = el('tr');
      head.append(el('th', '', '规则号'));
      head.append(el('th', '', '行为'));
      head.append(el('th', '', '类别'));
      head.append(el('th', '', '说明'));
      table.append(head);
      for (const row of payload.rows || []) {
        const tr = el('tr');
        tr.append(el('td', '', row.id));
        tr.append(el('td', '', row.item));
        const statusCell = el('td');
        statusCell.append(el('span', `badge-status ${row.status}`, labels[row.status] || row.status));
        tr.append(statusCell);
        tr.append(el('td', '', row.note));
        table.append(tr);
      }
      body.append(table);
    }

    /* ----------------------------------------------------- diagnostics */
    renderDiagnostics(world, info) {
      const body = $('diag-body');
      body.replaceChildren();
      const unknown = Array.from(world.diagnostics.unknownKinds);
      const uncovered = Array.from(world.diagnostics.uncoveredActions);
      const card = (title, lines, kind) => {
        const node = el('div', `diag-card${kind ? ` ${kind}` : ''}`);
        node.append(el('b', '', title));
        const list = el('div');
        list.style.marginTop = '4px';
        for (const line of lines) list.append(el('div', '', line));
        node.append(list);
        body.append(node);
      };
      card('运行计数', [
        `已记录帧 ${world.frameCount} / 最多 ${HW.OFFICIAL.maxRounds}`,
        `渲染单位 ${world.actors.length} · 特效 ${info.effects}（上限 ${HW.MAX_EFFECTS}，丢弃 ${info.dropped}）`,
        `帧率 ${info.fps.toFixed(1)} FPS · 单帧渲染 ${info.frameMs.toFixed(2)} ms`,
        `接口耗时：场景 ${info.timings.scenario.toFixed(0)} ms · 单步 ${info.timings.step.toFixed(0)} ms · 记录 ${info.timings.series.toFixed(0)} ms`,
      ]);
      card('未建模的单位类型', unknown.length ? unknown.map((k) => `${k}：使用降级图元显示`) : ['无。所有出现的类型都有专用画法。'],
        unknown.length ? 'warn' : '');
      card('观测到的未覆盖官方动作', uncovered.length
        ? uncovered.map((a) => `${a}：本地面板不模拟该动作的结算，仅记录`)
        : ['无。当前场景只提交了 move / attack / build / collect。'],
      uncovered.length ? 'warn' : '');
      card('仍然未实现（不会显示成可用）', HW.UNCOVERED.slice());
      const failures = world.diagnostics.notes.slice(-6);
      card('失败与异常', failures.length ? failures : ['没有失败指令记录。'], failures.length ? 'bad' : '');
      card('数据来源分离', [
        '画面与事件：本地 /debug/step 或 /debug/series（本地模拟，非官方判题）。',
        '策略输入：只有官方观测字段；_demo 私有状态在进入策略前被剥离。',
      ]);
    }

    /**
     * Blocking work: creating a scene, recording, importing. The transport is
     * locked because the world itself is being replaced.
     */
    setBusy(busy) {
      const next = Boolean(busy);
      if (this.busy === next) return;
      this.busy = next;
      for (const id of ['newmatch', 'preset', 'record', 'recordfull', 'play', 'step',
        'stepback', 'reset', 'speed', 'seed', 'side', 'pressure', 'apply-json', 'decide-json',
        'sample', 'import', 'import-replay', 'export', 'empty-new', 'empty-sample', 'scrub', 'seek-go']) {
        setDisabled($(id), next);
      }
      if (HW.experience && HW.app) HW.experience.update(HW.app.world || HW.app.placeholderWorld());
    }

    /**
     * One routine /debug/step in flight. This is the normal playback path, so it
     * must not grey anything out: pause, zoom, menus and an in-progress seek all
     * stay usable while the round settles.
     */
    setStepPending(pending) {
      const next = Boolean(pending);
      if (this.stepPending === next) return;
      this.stepPending = next;
      const root = $('app');
      if (root) root.dataset.stepPending = String(next);
      const step = $('step');
      if (step) step.setAttribute('aria-busy', String(next));
    }

    collapse(collapsed) {
      $('dev').classList.toggle('collapsed', collapsed);
      $('app').classList.toggle('debug-open', !collapsed);
      const toggle = $('debug-toggle');
      if (toggle) {
        toggle.textContent = collapsed ? '调试面板' : '收起调试面板';
        toggle.setAttribute('aria-expanded', String(!collapsed));
      }
      $('collapse').textContent = collapsed ? '展开面板' : '收起面板';
    }

    activateTab(name) {
      for (const tab of document.querySelectorAll('.tab')) {
        tab.classList.toggle('active', tab.dataset.tab === name);
        tab.setAttribute('aria-selected', String(tab.dataset.tab === name));
      }
      for (const pane of document.querySelectorAll('.pane')) {
        pane.classList.toggle('active', pane.dataset.pane === name);
      }
    }
  }

  HW.Panel = Panel;
  HW.EVENT_TYPES = EVENT_TYPES;
}(window));
