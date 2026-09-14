/* Constants and shared metadata.
 *
 * Two rules drive everything in this file:
 *   1. Nothing here may change the game. Values are either official constants
 *      (mirrored from docs/任务书.md, docs/接口文档.md v1.0) or pure presentation
 *      choices (colours, sizes, animation timings).
 *   2. Values the viewer could get wrong are fetched from /debug/stats at
 *      runtime and fall back to these mirrors only if that request fails.
 */
(function (global) {
  'use strict';

  const HW = global.HW = global.HW || {};

  // ---- official geometry and pacing (R02) --------------------------------
  const OFFICIAL = {
    width: 41,
    height: 32,
    dayRounds: 70,
    nightRounds: 60,
    roundsPerDay: 130,
    maxRounds: 1300,
    weaponCost: 25,
    towerLimit: 3,
    towerTypes: ['gatling', 'railgun', 'rocket'],
    towerRangeByLevel: { gatling: [3, 5, 7], railgun: [6, 8, 10], rocket: [10, 15, 1000000000] },
    healthByLevel: {
      station: [1500, 3000, 4500],
      gatling: [1000, 1500, 2000],
      railgun: [1000, 1500, 2000],
      rocket: [1000, 1500, 2000],
      wall: [1000, 1500, 2000],
    },
    unitHealth: { worker: 220, pioneer: 200, smallRobot: 40, middleRobot: 60, largeRobot: 500, bossRobot: 800 },
    robotAttack: { smallRobot: 5, middleRobot: 10, largeRobot: 20, bossRobot: 40 },
    robotScore: { smallRobot: 1, middleRobot: 2, largeRobot: 4, bossRobot: 10 },
  };

  // ---- labels -------------------------------------------------------------
  const KIND_NAMES = {
    station: '基地', worker: '工人', pioneer: '开拓者',
    gatling: '加特林炮台', railgun: '电磁狙击炮', rocket: '火箭发射台',
    wall: '围墙',
    stone: '石矿', iron: '铁矿', copper: '铜矿',
    vendor: '小贩', weaponShop: '武器商店',
    challengerTaskPoint1: '任务点一（蓝方）', challengerTaskPoint2: '任务点二（蓝方）',
    defenderTaskPoint1: '任务点一（红方）', defenderTaskPoint2: '任务点二（红方）',
    smallRobot: '小型机器人', middleRobot: '中型机器人',
    largeRobot: '大型机器人', bossRobot: 'BOSS 机器人',
  };
  const ACTION_NAMES = {
    move: '移动', attack: '攻击', build: '建造', collect: '采集', sell: '出售',
    buy: '购买', remove: '拆除', use: '使用', drop: '丢弃',
    acceptTask: '领取任务', submitAnswer: '提交答案',
    summonTreasure: '召唤宝藏', prompt: '询问',
  };
  const MATERIAL_NAMES = { stone: '石头', iron: '铁', copper: '铜' };
  // Official shop item names (任务书 §4.6.3), spelled exactly as published.
  const ITEM_NAMES = {
    WeaponUpgradeVoucher1: '武器升级券1 (Lv1→2)',
    WeaponUpgradeVoucher2: '武器升级券2 (Lv2→3)',
    WallUpgradeVoucher1: '围墙升级券1 (Lv1→2)',
    WallUpgradeVoucher2: '围墙升级券2 (Lv2→3)',
    StationUpgradeVoucher1: '基地升级券1 (Lv1→2)',
    StationUpgradeVoucher2: '基地升级券2 (Lv2→3)',
    WallFixer: '围墙修复包',
    Medicine: '生命药剂',
    DizzyWeapon: '眩晕法宝',
    Bomb: '范围炸弹',
    SmallRobotSummonOrder: '小型机器人召唤令',
    MiddleRobotSummonOrder: '中型机器人召唤令',
    LargeRobotSummonOrder: '大型机器人召唤令',
    BossRobotSummonOrder: 'BOSS 召唤令',
    AcientTablet: '古符石板',
    StarSand: '星辰之沙',
    FlameBreath: '烈焰之息',
    FrostPotion: '寒霜药剂',
    ThornAmulet: '荆棘护符',
    IronWhistle: '回音铁哨',
  };
  // Action kinds the local simulator can execute today. Anything else the
  // policy submits is reported as uncovered instead of being drawn as success.
  const EXECUTABLE_ACTIONS = ['move', 'attack', 'build', 'collect', 'sell', 'buy',
    'use', 'remove'];

  // The eleven official actions (R01). Anything outside this set is a protocol
  // anomaly, not "an unsupported extra feature".
  const OFFICIAL_ACTIONS = ['move', 'attack', 'sell', 'buy', 'build', 'remove',
    'acceptTask', 'submitAnswer', 'summonTreasure', 'use', 'drop', 'collect'];

  // Actions the local simulator executes today; the rest are reported as
  // uncovered instead of being drawn as if they worked.
  const IMPLEMENTED_ACTIONS = ['move', 'attack', 'build', 'collect', 'sell', 'buy',
    'use', 'remove'];

  const UNCOVERED = [
    '任务领取 / 答案提交 / LLM 与沙盒执行',
    '召唤宝藏（献祭物品与祭坛）',
    '加特林 90° 锥形与电磁炮穿透弹道',
    '双队同时对抗与官方胜负结算',
  ];

  // ---- palette: "sci-fi wasteland defence" --------------------------------
  const PALETTE = {
    ground0: '#0b1420', ground1: '#0f1b28',
    grid: 'rgba(120, 168, 210, 0.055)', grid5: 'rgba(130, 190, 235, 0.11)',
    own: '#4fe0b4', ownDeep: '#0f6b57', ownGlow: 'rgba(79, 224, 180, 0.5)',
    enemy: '#ff6b81', enemyDeep: '#7d2433', enemyGlow: 'rgba(255, 107, 129, 0.45)',
    robot: '#ffb347', robotDeep: '#8a5410',
    neutral: '#8fa3bb', neutralDeep: '#3c4a5c',
    night: '#0a1a3a', dusk: '#3a1e4e', dawn: '#5a3a2e', day: 'rgba(0,0,0,0)',
    ownTint: '#7ef0d0', enemyTint: '#ff9aa8',
    hp: '#6ef2a4', hpWarn: '#ffd166', hpBad: '#ff6b81',
    shield: '#7fd4ff', hot: '#fff3c4', steel: '#93a6ba', dark: '#111a26',
  };

  const ZONE_STYLE = {
    stone: { label: '石矿', color: '#9fb2c8', accent: '#d8e6f5', glyph: '石' },
    iron: { label: '铁矿', color: '#c98b6b', accent: '#f0c3a5', glyph: '铁' },
    copper: { label: '铜矿', color: '#d9a05b', accent: '#ffd9a0', glyph: '铜' },
    vendor: { label: '小贩', color: '#b7a5ff', accent: '#e3dbff', glyph: '贩' },
    weaponShop: { label: '武器商店', color: '#8fd0ff', accent: '#d6efff', glyph: '店' },
    challengerTaskPoint1: { label: '蓝方任务点一', color: '#5ad6ff', accent: '#c8f1ff', glyph: '任' },
    challengerTaskPoint2: { label: '蓝方任务点二', color: '#5ad6ff', accent: '#c8f1ff', glyph: '任' },
    defenderTaskPoint1: { label: '红方任务点一', color: '#ff8f6b', accent: '#ffd9c8', glyph: '任' },
    defenderTaskPoint2: { label: '红方任务点二', color: '#ff8f6b', accent: '#ffd9c8', glyph: '任' },
  };

  // Task point 2 occupies two cells (R07 / D06); used for the visual footprint.
  const ZONE_FOOTPRINT = { challengerTaskPoint2: 2, defenderTaskPoint2: 2 };

  const OWNER_COLORS = {
    own: { main: PALETTE.own, deep: PALETTE.ownDeep, glow: PALETTE.ownGlow, label: '我方' },
    enemy: { main: PALETTE.enemy, deep: PALETTE.enemyDeep, glow: PALETTE.enemyGlow, label: '敌方' },
    robot: { main: PALETTE.robot, deep: PALETTE.robotDeep, glow: 'rgba(255,179,71,0.45)', label: '机器人' },
    neutral: { main: PALETTE.neutral, deep: PALETTE.neutralDeep, glow: 'rgba(143,163,187,0.35)', label: '中立' },
  };

  // ---- helpers ------------------------------------------------------------
  function clamp(value, low, high) {
    return value < low ? low : (value > high ? high : value);
  }
  function lerp(a, b, t) { return a + (b - a) * t; }
  function easeInOut(t) { return t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2; }
  function easeOut(t) { return 1 - Math.pow(1 - t, 3); }

  function kindName(kind) { return KIND_NAMES[kind] || kind || '未知'; }
  function actionName(action) { return ACTION_NAMES[action] || action || '未知动作'; }
  function itemName(item) { return ITEM_NAMES[item] || item || '未知物品'; }
  function cellLabel(pos) { return pos ? `(${pos.x}, ${pos.y})` : '—'; }

  function hexToRgb(hex) {
    const raw = hex.replace('#', '');
    const full = raw.length === 3 ? raw.split('').map((c) => c + c).join('') : raw;
    const num = parseInt(full, 16);
    return [(num >> 16) & 255, (num >> 8) & 255, num & 255];
  }
  function rgba(hex, alpha) {
    const [r, g, b] = hexToRgb(hex);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }
  function mix(hexA, hexB, t) {
    const a = hexToRgb(hexA); const b = hexToRgb(hexB);
    return `rgb(${Math.round(lerp(a[0], b[0], t))}, ${Math.round(lerp(a[1], b[1], t))}, ${Math.round(lerp(a[2], b[2], t))})`;
  }
  function formatSeconds(ms) { return `${(ms / 1000).toFixed(2)}s`; }

  // ---- day / night from the round number (official split) -----------------
  function phaseInfo(roundNo) {
    const round = clamp(Number(roundNo) || 1, 1, OFFICIAL.maxRounds);
    const index = (round - 1) % OFFICIAL.roundsPerDay;
    const day = Math.floor((round - 1) / OFFICIAL.roundsPerDay) + 1;
    const isDay = index < OFFICIAL.dayRounds;
    const inPhase = isDay ? index + 1 : index - OFFICIAL.dayRounds + 1;
    const total = isDay ? OFFICIAL.dayRounds : OFFICIAL.nightRounds;
    const ratio = inPhase / total;
    // 0 = full day, 1 = full night, with smooth dusk/dawn shoulders for art only.
    let darkness;
    if (isDay) darkness = clamp((ratio - 0.82) / 0.18, 0, 1) * 0.55;
    else darkness = clamp(1 - (1 - ratio) * 0.25, 0.55, 1);
    const dusk = isDay && ratio > 0.82 ? (ratio - 0.82) / 0.18 : (!isDay && ratio < 0.12 ? 1 - ratio / 0.12 : 0);
    const dawn = !isDay && ratio > 0.88 ? (ratio - 0.88) / 0.12 : 0;
    return {
      round, day, isDay, inPhase, total, ratio, darkness, dusk, dawn,
      label: isDay ? '白天' : '夜晚',
      phaseLabel: isDay ? '昼' : '夜',
      tint: isDay ? 'day' : 'night',
    };
  }

  function healthMax(kind, level) {
    const table = OFFICIAL.healthByLevel[kind];
    if (table) {
      const idx = clamp((Number(level) || 1) - 1, 0, table.length - 1);
      return table[idx];
    }
    return OFFICIAL.unitHealth[kind] || 100;
  }

  function rangeOf(kind, level) {
    const table = OFFICIAL.towerRangeByLevel[kind];
    if (!table) return 0;
    const idx = clamp((Number(level) || 1) - 1, 0, table.length - 1);
    return table[idx];
  }

  HW.OFFICIAL = OFFICIAL;
  HW.PALETTE = PALETTE;
  HW.ZONE_STYLE = ZONE_STYLE;
  HW.ZONE_FOOTPRINT = ZONE_FOOTPRINT;
  HW.OWNER_COLORS = OWNER_COLORS;
  HW.KIND_NAMES = KIND_NAMES;
  HW.ACTION_NAMES = ACTION_NAMES;
  HW.MATERIAL_NAMES = MATERIAL_NAMES;
  HW.ITEM_NAMES = ITEM_NAMES;
  HW.EXECUTABLE_ACTIONS = EXECUTABLE_ACTIONS;
  HW.OFFICIAL_ACTIONS = OFFICIAL_ACTIONS;
  HW.IMPLEMENTED_ACTIONS = IMPLEMENTED_ACTIONS;
  HW.UNCOVERED = UNCOVERED;
  HW.util = {
    clamp, lerp, easeInOut, easeOut, kindName, actionName, itemName, cellLabel,
    rgba, mix, hexToRgb, formatSeconds,
  };
  HW.phaseInfo = phaseInfo;
  HW.healthMax = healthMax;
  HW.rangeOf = rangeOf;
  HW.runtime = { stats: Object.assign({}, OFFICIAL), statsLoaded: false, rules: null };
}(window));
