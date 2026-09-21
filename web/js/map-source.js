/* Source labels describe stored data, never the currently selected new-game option. */
(function (global) {
  'use strict';
  const HW = global.HW = global.HW || {};
  function describe(world) {
    if (!world) return '地图来源：尚未创建。已观测布局只校准基地、商贩和任务点；地形资源等仍待补证。';
    const state = world.state || (world.states && world.states[0]) || {};
    const layout = (state._demo && state._demo.map_layout)
      || (world.metadata && world.metadata.mapLayout);
    const id = typeof layout === 'string' ? layout : layout && layout.id;
    const source = world.mode === 'record' ? '本地录像回看（保留录制坐标）'
      : world.mode === 'sample' ? '导入 / 示例单帧（保留原坐标）' : '本地重模拟';
    const detail = id === 'attack-map-observed-v1'
      ? 'attack_map：五场首帧已观测基地、商贩、任务点；地形资源、完整出生和刷怪位置等仍含本地假设，未完成全图对齐。'
      : id === 'seeded-local-v1' ? '随机布局：本地压力测试，不用于官方地图对齐。'
        : '此快照未记录可识别的地图档，按原数据显示，不迁移到新布局。';
    return `${source} · ${detail} 本地生成不等于官方回放，同回合建筑数量可能不同。`;
  }
  HW.mapSource = { describe };
}(window));
