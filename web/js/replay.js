/* Local replay archive and navigation. Never calls the strategy or simulator. */
(function (global) {
  'use strict';
  const HW = global.HW;
  const KIND = 'competition-hw-replay';
  const object = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
  const pos = (v) => object(v) && Number.isFinite(v.x) && Number.isFinite(v.y);
  function validate(record) {
    if (!object(record) || !Array.isArray(record.frames) || !Array.isArray(record.states))
      throw new Error('录像需要完整 frames 和 states；旧版单帧摘要不是录像');
    if (record.frames.length > 1300 || record.states.length !== record.frames.length + 1)
      throw new Error('录像缺帧或超出 1300 回合：状态数量应比事件帧多一');
    if (typeof record.done !== 'boolean') throw new Error('录像缺少终局标记');
    record.states.forEach((state, index) => {
      if (!object(state) || !object(state.mapInfo) || !object(state.teamOur)
          || !Number.isInteger(state.roundNo) || state.roundNo < 1 || state.roundNo > 1301)
        throw new Error(`第 ${index} 帧状态无效`);
      if (state.mapInfo.width !== 41 || state.mapInfo.height !== 32)
        throw new Error(`第 ${index} 帧地图应为 41 × 32`);
      if (!Array.isArray(state.teamOur.roles)) throw new Error(`第 ${index} 帧缺少我方角色`);
      if (!Array.isArray(state.mapInfo.zones) || state.mapInfo.zones.some((zone) => !object(zone) || !pos(zone.pos)))
        throw new Error(`第 ${index} 帧地图中立点无效`);
      for (const group of [state.teamOur, state.teamEnemy, state.robot]) {
        if (group && !Array.isArray(group.roles)) throw new Error(`第 ${index} 帧角色列表无效`);
        for (const unit of (group && group.roles) || []) {
          if (!object(unit) || !pos(unit.pos) || typeof unit.roleType !== 'string'
              || (unit.backpack != null && !Array.isArray(unit.backpack)))
            throw new Error(`第 ${index} 帧单位坐标无效`);
        }
      }
      if (index && state.roundNo < record.states[index - 1].roundNo)
        throw new Error('录像回合顺序倒退');
    });
    record.frames.forEach((frame, index) => {
      if (!object(frame) || frame.round !== record.states[index].roundNo)
        throw new Error(`第 ${index + 1} 帧事件与结算前回合不匹配`);
      for (const key of ['actions', 'robotMoves', 'robotAttacks', 'unitDeaths', 'robotDeaths', 'spawned', 'skipped', 'moved']) {
        if (frame[key] != null && !Array.isArray(frame[key])) throw new Error(`帧字段 ${key} 应为列表`);
        if (key !== 'skipped' && (frame[key] || []).some((entry) => !object(entry)))
          throw new Error(`帧字段 ${key} 的事件无效`);
      }
      for (const key of ['commands', 'executed']) {
        if (frame[key] != null && !object(frame[key])) throw new Error(`帧字段 ${key} 应为指令映射`);
        for (const command of Object.values(frame[key] || {})) {
          if (!object(command) || (command.targetPos != null && (!Array.isArray(command.targetPos) || !command.targetPos.every(pos))))
            throw new Error('录像指令目标坐标无效');
        }
      }
    });
    return record;
  }
  function capture(world) {
    return {
      seed: world.seed, side: world.side, pressure: world.pressure,
      frames: world.frames, states: world.states, done: Boolean(world.done),
      metadata: world.metadata || {},
    };
  }
  async function digest(record) {
    const bytes = new TextEncoder().encode(JSON.stringify(record));
    return Array.from(new Uint8Array(await global.crypto.subtle.digest('SHA-256', bytes)),
      (n) => n.toString(16).padStart(2, '0')).join('');
  }
  async function pack(world) {
    const recording = validate(capture(world));
    return { kind: KIND, version: 1, createdAt: new Date().toISOString(), local: true,
      note: '本地录像，包含所有已记录状态与事件；不是官方回放。',
      checksum: await digest(recording), recording };
  }
  async function unpack(archive) {
    if (!archive || archive.kind !== KIND || archive.version !== 1)
      throw new Error('不支持的录像格式或版本');
    if (archive.checksum !== await digest(archive.recording))
      throw new Error('录像校验失败，文件可能被修改或损坏');
    return validate(archive.recording);
  }
  function markers(world) {
    const rows = [];
    world.frames.forEach((frame, offset) => {
      const types = new Set();
      if ((frame.skipped || []).length) types.add('skip');
      if ((frame.unitDeaths || []).length) types.add('death');
      if ((frame.spawned || []).length) types.add('spawn');
      if ((frame.actions || []).some((a) => ['acceptTask', 'submitAnswer', 'summonTreasure'].includes(a.a))) types.add('task');
      const before = HW.phaseInfo(world.states[offset].roundNo);
      const after = HW.phaseInfo(world.states[offset + 1].roundNo);
      if (before.day !== after.day || before.label !== after.label) types.add('phase');
      if (types.size) rows.push({ index: offset + 1, round: frame.round, types: Array.from(types) });
    });
    return rows;
  }
  function nextMarker(rows, index, direction, filter) {
    const candidates = rows.filter((r) => (filter === 'all' || r.types.includes(filter))
      && (direction > 0 ? r.index > index : r.index < index));
    return candidates.length ? candidates[direction > 0 ? 0 : candidates.length - 1].index : null;
  }
  HW.Replay = { KIND, validate, capture, pack, unpack, markers, nextMarker };
}(window));
