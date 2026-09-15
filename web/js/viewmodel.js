/* View model: turns a serialized local state + recorded frame into actors.
 *
 * Fidelity rules enforced here:
 *   - Actor positions are the committed logical positions from the state.
 *     Animation only interpolates towards a value the simulator already
 *     committed, so a moving sprite is never ahead of the real coordinate.
 *   - Every animation has a cause in the frame record (moved / actions /
 *     robotMoves / robotAttacks / unitDeaths / robotDeaths / spawned / gold).
 *   - An action reported as failed (`executed` result false) is never drawn as
 *     a success; the dev panel lists it as a failure instead.
 */
(function (global) {
  'use strict';

  const HW = global.HW = global.HW || {};
  const U = HW.util;
  const PALETTE = HW.PALETTE;
  const OWNER_COLORS = HW.OWNER_COLORS;
  const ZONE_STYLE = HW.ZONE_STYLE;

  const ZONE_KINDS = Object.keys(ZONE_STYLE);
  const BUILDING_KINDS = ['station', 'gatling', 'railgun', 'rocket', 'wall'];
  const CREW_KINDS = ['worker', 'pioneer'];
  // Largest jump that still reads as "walking"; anything bigger is a teleport
  // (revive, wave spawn, enemy id reuse) and must snap instead of gliding.
  const MAX_WALK_STEP = 2;

  function footprintOf(kind) {
    if (kind === 'station') return 2;
    return HW.ZONE_FOOTPRINT[kind] || 1;
  }

  function isUnmodelled(kind) {
    return !ZONE_KINDS.includes(kind) && !BUILDING_KINDS.includes(kind)
      && !CREW_KINDS.includes(kind) && !(kind || '').endsWith('Robot');
  }

  function makeActor(unit, owner, kind) {
    const actor = {
      id: unit.id,
      key: unit.key || `unit:${owner}:${unit.id}`,
      uid: unit.uid,
      owner,
      kind,
      pos: { x: unit.pos.x, y: unit.pos.y },
      rpos: { x: unit.pos.x, y: unit.pos.y },
      level: unit.level || 1,
      health: Number(unit.health != null ? unit.health : 1),
      maxHealth: owner === 'robot'
        ? (HW.OFFICIAL.unitHealth[kind] || 40)
        : HW.healthMax(kind, unit.level || 1),
      backpack: unit.backpack || [],
      capacity: unit.backPackCapability || 0,
      cooldown: unit.cooldown || 0,
      abnormalState: unit.abnormalState || '',
      anim: [],
      selected: false,
      hitFlash: 0,
      facing: 0,
      recoil: 0,
      muzzle: 0,
    };
    actor.size = footprintOf(kind);
    actor.label = U.kindName(kind);
    actor.color = OWNER_COLORS[owner] || OWNER_COLORS.neutral;
    actor.unmodelled = isUnmodelled(kind);
    return actor;
  }

  /* ---------------------------------------------------------------- zones */

  function buildZones(state) {
    const zones = [];
    const list = (state.mapInfo && state.mapInfo.zones) || [];
    for (const zone of list) {
      const kind = zone.neutralType;
      const style = ZONE_STYLE[kind];
      zones.push({
        kind,
        pos: { x: zone.pos.x, y: zone.pos.y },
        size: footprintOf(kind),
        label: style ? style.label : kind,
        color: style ? style.color : PALETTE.neutral,
        accent: style ? style.accent : PALETTE.neutral,
        glyph: style ? style.glyph : '?',
        known: Boolean(style),
      });
    }
    return zones;
  }

  /* --------------------------------------------------------------- actors */

  function actorsFromState(state) {
    const actors = [];
    const groups = [
      [(state.teamOur && state.teamOur.roles) || [], 'own'],
      [(state.teamEnemy && state.teamEnemy.roles) || [], 'enemy'],
      [(state.robot && state.robot.roles) || [], 'robot'],
    ];
    for (const [roles, owner] of groups) {
      for (const unit of roles) {
        if (!unit || !unit.pos) continue;
        if ((unit.health || 0) <= 0) continue;
        actors.push(makeActor(unit, owner, unit.roleType));
      }
    }
    return actors;
  }

  function positionMap(state) {
    const map = new Map();
    for (const actor of actorsFromState(state)) {
      map.set(actor.key, { x: actor.pos.x, y: actor.pos.y });
    }
    return map;
  }

  /* ---------------------------------------------------------------- world */

  /**
   * One match: states, recorded frames and the currently rendered frame.
   * Index i always means "after frame i executed"; index 0 is the initial
   * layout before any command was submitted.
   */
  class World {
    constructor() {
      this.seed = 1;
      this.side = 'challenger';
      this.pressure = 1;
      this.profile = 'observed-seven-days';
      this.states = [];
      this.frames = [];
      this.done = false;
      this.index = 0;
      this.actors = [];
      this.zones = [];
      this.deaths = [];
      this.phase = HW.phaseInfo(1);
      this.diagnostics = { unknownKinds: new Set(), uncoveredActions: new Set(), notes: [] };
      this.positions = new Map();
      this.kinds = new Map();
      this.gold = 0;
      this.score = 0;
      this.kills = 0;
      this.mode = 'idle';
      this.positionsCache = [];
    }

    static fromScenario(payload) {
      const world = new World();
      const state = payload.state;
      world.seed = (state._demo && state._demo.seed) || 1;
      world.side = state.teamOur.type;
      world.pressure = (state._demo && state._demo.pressure) || 1;
      // A snapshot generated before Issue19 has no `profile`; label it as legacy
      // instead of showing the new default, which would misdescribe its waves.
      world.profile = (state._demo && state._demo.profile) || 'legacy-unknown';
      world.states = [state];
      world.metadata = payload.metadata || {};
      world.frames = [];
      world.index = 0;
      world.mode = 'live';
      world.positionsCache = [positionMap(state)];
      world.applyFrame(0, { instant: true });
      return world;
    }

    static fromRecording(recording) {
      const world = new World();
      world.seed = recording.seed;
      world.side = recording.side;
      world.pressure = recording.pressure;
      world.profile = recording.profile
        || (recording.initial && recording.initial._demo && recording.initial._demo.profile)
        || 'legacy-unknown';
      world.metadata = recording.metadata || {};
      const frames = recording.frames || [];
      const provided = recording.states || null;
      const states = provided && provided.length === frames.length + 1
        ? provided.slice()
        : [recording.initial].concat(frames.map((frame) => frame && frame.state));
      // A malformed payload must degrade, not crash the viewer.
      for (let i = 0; i < states.length; i += 1) {
        if (!states[i] || !states[i].teamOur) {
          const fallback = states[i - 1] || recording.initial;
          world.diagnostics.notes.push(`记录缺少第 ${i} 帧状态，已用上一帧代替显示`);
          states[i] = fallback;
        }
      }
      world.states = states;
      world.frames = frames;
      world.done = Boolean(recording.done);
      world.mode = 'record';
      world.positionsCache = world.states.map(positionMap);
      world.index = 0;
      world.applyFrame(world.index, { instant: true });
      return world;
    }

    /** Live mode: append one /debug/step result and render the new frame. */
    pushStep(result) {
      if (result.frame) {
        result.frame.commands = result.roleCommandMap || {};
        result.frame.executed = result.executed || {};
        this.frames.push(result.frame);
        this.states.push(result.state);
        this.positionsCache.push(positionMap(result.state));
        this.done = Boolean(result.done);
        this.applyFrame(this.frameCount, { instant: false });
      } else {
        this.states[this.states.length - 1] = result.state;
        this.applyFrame(this.index, { instant: true });
      }
    }

    get frameCount() { return this.frames.length; }
    get maxIndex() { return Math.max(0, this.states.length - 1); }
    get state() { return this.states[U.clamp(this.index, 0, this.states.length - 1)] || this.states[0]; }
    get frame() { return this.index === 0 ? null : (this.frames[this.index - 1] || null); }
    get roundNo() { return (this.state && this.state.roundNo) || 1; }

    /**
     * Render the match state at `index`.
     * instant=true snaps to committed positions; instant=false starts actors at
     * their committed position in the previous state so the renderer can tween
     * the recorded walk.
     */
    applyFrame(index, options) {
      const opts = options || {};
      const target = U.clamp(index, 0, this.maxIndex);
      this.index = target;
      const state = this.states[target];
      if (!state) return;
      const frame = target === 0 ? null : this.frames[target - 1];

      this.phase = HW.phaseInfo(state.roundNo);
      this.zones = buildZones(state);
      const built = actorsFromState(state);

      // Hit / death keys straight from the recorded frame.
      const hitKeys = new Set();
      const killedKeys = new Set();
      const spawnedKeys = new Set();
      if (frame) {
        for (const action of frame.actions || []) {
          for (const salvo of action.salvos || []) {
            for (const hit of salvo.hits || []) hitKeys.add(`unit:robot:${hit.robot}`);
          }
        }
        for (const death of frame.robotDeaths || []) killedKeys.add(`unit:robot:${death.robot}`);
        for (const death of (frame.unitDeaths || []).concat(frame.stationDeaths || [])) {
          killedKeys.add(`unit:own:${death.id}`);
          killedKeys.add(`unit:enemy:${death.id}`);
        }
        for (const spawn of frame.spawned || []) spawnedKeys.add(`unit:robot:${spawn.robot}`);
        for (const attack of frame.robotAttacks || []) {
          // Resolve the victim by looking at both owned roles, never by id range.
          const own = ((state.teamOur && state.teamOur.roles) || []).some((r) => r.id === attack.victim);
          hitKeys.add(`unit:${own ? 'own' : 'enemy'}:${attack.victim}`);
        }
      }

      const previousPositions = this.positionsCache[target - 1]
        || (target > 0 ? positionMap(this.states[target - 1]) : null);
      const attackByTower = new Map();
      for (const action of (frame && frame.actions) || []) {
        if (action.a === 'attack') attackByTower.set(action.id, action);
      }

      for (const actor of built) {
        const previous = previousPositions && previousPositions.get(actor.key);
        const previousKind = this.kinds.get(actor.key);
        const distance = previous
          ? Math.max(Math.abs(previous.x - actor.pos.x), Math.abs(previous.y - actor.pos.y))
          : 0;
        if (!opts.instant && previous && distance > 0 && distance <= MAX_WALK_STEP) {
          actor.rpos = { x: previous.x, y: previous.y };
          actor.anim.push({
            type: 'walk',
            from: { x: previous.x, y: previous.y },
            to: { x: actor.pos.x, y: actor.pos.y },
          });
        }
        if (previousKind && previousKind !== actor.kind) {
          actor.anim.push({ type: 'transform', from: previousKind, to: actor.kind });
        }
        if (spawnedKeys.has(actor.key)) actor.anim.push({ type: 'arrive' });
        if (killedKeys.has(actor.key)) actor.anim.push({ type: 'destroyed' });
        if (hitKeys.has(actor.key)) actor.hitFlash = 1;
        const shot = attackByTower.get(actor.id);
        if (shot) {
          actor.muzzle = 1;
          actor.lastShot = shot;
        }
        if (actor.unmodelled) this.diagnostics.unknownKinds.add(actor.kind);
      }

      // Units that left the state (killed, cleared at dawn) keep a short death
      // animation instead of vanishing between two frames.
      const aliveKeys = new Set(built.map((a) => a.key));
      const deaths = [];
      for (const actor of this.actors) {
        if (aliveKeys.has(actor.key)) continue;
        if (this.index === 0) continue;
        deaths.push({
          key: actor.key, kind: actor.kind, owner: actor.owner,
          pos: { x: actor.rpos.x, y: actor.rpos.y }, level: actor.level,
        });
      }
      this.deaths = deaths;
      this.kinds = new Map(built.map((a) => [a.key, a.kind]));
      this.actors = built;
      this.gold = Number((state.teamOur && state.teamOur.goldNum) || 0);
      this.score = Number((state.teamOur && state.teamOur.totalScore) || 0);
      this.kills = Number((state._demo && state._demo.kills) || 0);
      this.noteUncovered(frame);
    }

    /** Track official actions this local build does not execute yet. */
    noteUncovered(frame) {
      if (!frame) return;
      for (const command of Object.values(frame.executed || {})) {
        if (command && command.action && !HW.IMPLEMENTED_ACTIONS.includes(command.action)) {
          this.diagnostics.uncoveredActions.add(command.action);
        }
      }
      const failed = (frame.skipped || []).length;
      if (failed) this.diagnostics.notes.push(`第 ${frame.round} 回合有 ${failed} 条指令未执行`);
      if (this.diagnostics.notes.length > 40) this.diagnostics.notes.shift();
    }

    station() {
      return this.actors.find((a) => a.owner === 'own' && a.kind === 'station') || null;
    }

    stationHealth() {
      const roles = (this.state.teamOur && this.state.teamOur.roles) || [];
      const stations = roles.filter((r) => r.roleType === 'station');
      const alive = stations.filter((s) => (s.health || 0) > 0);
      const total = stations.reduce((sum, s) => sum + Math.max(0, s.health || 0), 0);
      const max = stations.reduce((sum, s) => sum + HW.healthMax('station', s.level || 1), 0);
      return {
        alive: alive.length,
        total,
        max: max || 1,
        dead: stations.length > 0 && alive.length === 0,
      };
    }

    counts() {
      const result = { own: 0, robots: 0, enemy: 0, workers: 0, pioneers: 0, towers: 0, walls: 0 };
      for (const actor of this.actors) {
        if (actor.owner === 'robot') { result.robots += 1; continue; }
        if (actor.owner === 'enemy') { result.enemy += 1; continue; }
        result.own += 1;
        if (actor.kind === 'worker') result.workers += 1;
        else if (actor.kind === 'pioneer') result.pioneers += 1;
        else if (HW.OFFICIAL.towerTypes.includes(actor.kind)) result.towers += 1;
        else if (actor.kind === 'wall') result.walls += 1;
      }
      return result;
    }

    /** Event log lines for frame `index`, derived from the recorded frame. */
    describe(index) {
      const lines = [];
      if (index <= 0) return lines;
      const frame = this.frames[index - 1];
      if (!frame) return lines;
      const label = (kind, id) => `${U.kindName(kind)} ${id}`;
      for (const action of frame.actions || []) {
        if (action.a === 'move') {
          lines.push({ type: 'move', text: `${label(action.kind, action.id)} 移动 ${U.cellLabel(action.from)} → ${U.cellLabel(action.to)}` });
        } else if (action.a === 'collect') {
          lines.push({ type: 'collect', text: `${label('worker', action.id)} 采集${HW.MATERIAL_NAMES[action.kind] || action.kind} ${U.cellLabel(action.to)}（背包 ${action.bag}/${action.cap}）` });
        } else if (action.a === 'build') {
          const cost = HW.OFFICIAL.towerTypes.includes(action.kind) ? `，花费 ${HW.OFFICIAL.weaponCost} 金币` : '，消耗石头 1';
          const extra = (action.replaced && action.replaced.length) ? `，覆盖原建筑 ${action.replaced.join(',')}` : '';
          lines.push({ type: 'build', text: `${label('worker', action.id)} 在 ${U.cellLabel(action.to)} 建造 ${U.kindName(action.kind)}${cost}${extra}` });
        } else if (action.a === 'attack') {
          const hits = (action.salvos || []).reduce((sum, s) => sum + (s.hits || []).length, 0);
          const damage = (action.salvos || []).reduce((sum, s) => sum + (s.hits || []).reduce((d, h) => d + h.damage, 0), 0);
          // The old wording claimed ballistic blocking was not modelled; gatling
          // cones, per-bullet path hits and railgun penetration are all computed
          // now (see agent/ballistics.py), so the text has to say what is real.
          const how = action.source === 'ballistics'
            ? '直线弹道（加特林逐发命中路径上最近的机器人；电磁炮按能量穿透）'
            : (action.source === 'missile' ? '火箭：中心 20，周围 8 格 10，不被阻挡' : '本地结算');
          lines.push({ type: 'attack', text: `${U.kindName(action.kind)}(Lv${action.level}) ${action.id} 由 ${label('worker', action.controller)} 操控开火：${hits} 次命中 / ${damage} 伤害（${how}）` });
        } else if (action.a === 'acceptTask') {
          lines.push({ type: 'task', text: `${label('pioneer', action.id)} 在任务点 ${U.cellLabel(action.point || action.to)} 领取任务` });
        } else if (action.a === 'submitAnswer') {
          lines.push({ type: 'task', text: `${label('pioneer', action.id)} 提交任务答案（${String(action.answer || '').length} 字符）` });
        } else if (action.a === 'summonTreasure') {
          lines.push({ type: 'gold', text: `${label('pioneer', action.id)} 在 ${U.cellLabel(action.to)} 献祭 ${(action.items || []).map((item) => U.itemName(item)).join('、')}，开启宝藏（积分 +${action.score}，金币 +${action.gold}）` });
        } else if (action.a === 'sell') {
          lines.push({ type: 'gold', text: `${label('worker', action.id)} 卖出${HW.MATERIAL_NAMES[action.kind] || action.kind}×${action.num}（单价 ${action.price}），+${action.gold} 金币` });
        } else if (action.a === 'buy') {
          lines.push({ type: 'gold', text: `${label('worker', action.id)} 购买${U.itemName(action.kind)}×${action.num}，-${Math.abs(action.gold)} 金币` });
        } else if (action.a === 'upgrade') {
          lines.push({ type: 'build', text: `${label('worker', action.id)} 使用${U.itemName(action.kind)}升级建筑 ${action.targetId} → Lv${action.level}，血量恢复至 ${action.health}` });
        } else if (action.a === 'remove') {
          lines.push({ type: 'build', text: `${label('worker', action.id)} 拆除围墙 ${action.removedId} ${U.cellLabel(action.to)}（不退还石头）` });
        } else if (action.a === 'bomb') {
          lines.push({ type: 'attack', text: `范围炸弹命中机器人 ${action.robot}，伤害 ${action.damage}（结算先于机器人移动）` });
        } else if (action.a === 'dizzy') {
          lines.push({ type: 'attack', text: `眩晕法宝命中机器人 ${action.robot}，眩晕 ${action.rounds} 回合` });
        } else if (action.a === 'dizzy_end') {
          lines.push({ type: 'robot', text: `机器人 ${action.robot} 眩晕结束` });
        } else if (action.a === 'use') {
          lines.push({ type: 'build', text: `${label('worker', action.id)} 使用${U.itemName(action.kind)}` });
        }
      }
      for (const move of frame.robotMoves || []) {
        lines.push({ type: 'robot', text: `${U.kindName(move.kind)} ${move.robot} 移动 ${U.cellLabel(move.from)} → ${U.cellLabel(move.to)}` });
      }
      for (const attack of frame.robotAttacks || []) {
        const target = attack.building != null
          ? `${attack.buildingKind ? U.kindName(attack.buildingKind) : '建筑'} ${attack.building}`
          : `角色 ${attack.victim}`;
        lines.push({ type: 'robot', text: `${U.kindName(attack.kind)} ${attack.robot} 攻击${target}，伤害 ${attack.damage}` });
      }
      for (const death of frame.unitDeaths || []) {
        lines.push({ type: 'death', text: `我方 ${U.kindName(death.kind)} ${death.id} 阵亡 ${U.cellLabel(death.pos)}` });
      }
      for (const death of frame.robotDeaths || []) {
        lines.push({ type: 'kill', text: `${U.kindName(death.kind)} ${death.robot} 被击毁 ${U.cellLabel(death.pos)}，击杀分 +${death.score}` });
      }
      const arrivals = frame.spawned || [];
      if (arrivals.length > 1) {
        const counts = new Map();
        for (const spawn of arrivals) counts.set(spawn.kind, (counts.get(spawn.kind) || 0) + 1);
        const detail = Array.from(counts, ([kind, count]) => `${U.kindName(kind)} ${count} 只`).join('、');
        lines.push({ type: 'spawn', text: `本轮生成 ${arrivals.length} 只机器人：${detail}` });
      }
      for (const spawn of arrivals) {
        lines.push({ type: 'spawn', text: `生成 ${U.kindName(spawn.kind)} ${spawn.robot} ${U.cellLabel(spawn.pos)}（本地模拟）` });
      }
      if (frame.gold && frame.gold.before !== frame.gold.after) {
        const delta = frame.gold.after - frame.gold.before;
        lines.push({ type: 'gold', text: `金币 ${frame.gold.before} → ${frame.gold.after}（${delta > 0 ? '+' : ''}${delta}）` });
      }
      for (const id of frame.skipped || []) {
        const command = (frame.executed || {})[id] || {};
        const what = command.action ? U.actionName(command.action) : '指令';
        lines.push({ type: 'skip', text: `角色 ${id} 的${what}未执行（本地规则校验失败或碰撞，不是协议异常）` });
      }
      return lines;
    }

    result() {
      const stations = (this.state.teamOur && this.state.teamOur.roles || [])
        .filter((r) => r.roleType === 'station');
      const alive = stations.some((s) => (s.health || 0) > 0);
      return {
        round: this.roundNo,
        day: this.phase.day,
        done: this.done && this.index === this.maxIndex,
        reason: !alive ? '我方基地被摧毁' : (this.roundNo >= HW.OFFICIAL.maxRounds ? '达到 1300 回合上限' : ''),
        score: this.score,
        kills: this.kills,
        gold: this.gold,
        station: this.stationHealth(),
        robots: ((this.state.robot && this.state.robot.roles) || []).length,
      };
    }
  }

  // Keys that describe *this round only* and must never be carried into the next
  // request. The browser posts the state it was given straight back to /debug/step,
  // so a stale within-round flag would silently change planning for the whole match
  // (measured: the treasure stopped opening in the viewer while working in-process).
  const roundScopedKeys = ['_treasureRound'];

  // Presentation only: read the selected snapshot, never infer solver completion
  // from elapsed time or inspect future frames / a fixture's reference answer.
  function taskStatus(world) {
    const state = (world && world.state) || {};
    const meta = state._demo || {};
    const description = String(state.phaseTask || '').trim();
    const report = meta.task_report || {};
    const points = (state.teamOur && state.teamOur.playerTasks) || [];
    const team = state.teamOur && state.teamOur.type;
    const books = (meta.task_world && meta.task_world.points) || {};
    const active = Object.entries(books).filter(([key]) => team && key.startsWith(team))
      .map(([, book]) => book.active).find((task) => task && task.description === description);
    if (description) {
      const total = active && Number(active.timeout);
      const deadline = active && Number(active.deadline);
      const round = Number(state.roundNo);
      const known = total > 0 && Number.isFinite(deadline) && Number.isFinite(round);
      const left = known ? U.clamp(deadline - round, 0, total) : null;
      return { active: true, title: active && active.pending_answer ? '答案已提交 · 等待结算' : '开拓者任务进行中',
        description: description.split('\n')[0], ratio: known ? left / total : null,
        meter: known ? `剩余 ${left} / ${total} 轮` : '未提供可靠的剩余轮数',
        hint: known ? '时间条表示剩余期限，不是解题完成率。来源：本地任务记录。' : '当前观测有任务描述，但没有接取时刻；不估算完成百分比。',
        compact: known ? `任务剩余 ${left} 轮` : '任务进行中' };
    }
    if (report.ended || report.rewards) {
      const reward = report.rewards;
      const hasRate = reward && reward.rate != null && Number.isFinite(Number(reward.rate));
      return { active: false, title: '任务已结束', description: String(report.ended || '已结算'),
        ratio: hasRate ? U.clamp(Number(reward.rate), 0, 1) : null,
        meter: hasRate ? `本地结算通过率 ${Math.round(Number(reward.rate) * 100)}%` : '未提供通过率',
        hint: reward ? `积分 +${reward.score ?? 0} · 金币 +${reward.gold ?? 0}（本地结算）` : '结束不等于全部答对；以结算结果为准。' };
    }
    const available = points.filter((p) => p.isValid).length;
    const cooling = points.map((p) => Number(p.coldDownRounds)).filter((n) => n > 0);
    return { active: false, title: '尚未领取任务', description: available ? `${available} 个任务点可领取` : cooling.length ? `任务点最早 ${Math.min(...cooling)} 轮后刷新` : '等待任务信息',
      ratio: null, meter: '暂无进行中的任务', hint: '开拓者负责接取与提交任务；工人负责采集、建造与防守。' };
  }

  HW.World = World;
  HW.viewModel = { actorsFromState, buildZones, footprintOf, positionMap, MAX_WALK_STEP,
                   isUnmodelled, describe: World.prototype.describe, roundScopedKeys, taskStatus };
}(window));
