/* Canvas renderer: terrain, structures, units, effects, overlays, lighting.
 *
 * The renderer owns no game state. It paints the World it is given and, for
 * animations, interpolates between positions the simulator already committed,
 * so the picture can never run ahead of the logic.
 */
(function (global) {
  'use strict';

  const HW = global.HW = global.HW || {};
  const U = HW.util;
  const PALETTE = HW.PALETTE;
  const S = HW.sprites;
  const P = S.primitives;

  // One tile size for the whole viewer. It is exported below so tests and the
  // effect layer use exactly the same value the renderer draws with.
  const BASE_TILE = 44;
  const MIN_SCALE = 0.15;
  const MAX_SCALE = 3;
  HW.BASE_TILE = BASE_TILE;

  function hash2(x, y) {
    const n = Math.sin(x * 127.1 + y * 311.7) * 43758.5453;
    return n - Math.floor(n);
  }

  /* ------------------------------------------------- day / night palettes
   *
   * Both the letterbox around the map ("sky") and the map surface itself follow
   * the phase of the round that is being rendered. The phase already comes from
   * official round data (HW.phaseInfo(state.roundNo)) and is stored on the World
   * when a frame is applied, so a replay seek restores exactly the palette that
   * frame had; nothing here reads the wall clock or a future frame.
   */
  const SKY_PALETTES = [
    { sky: ['#7fb3e0', '#cfd9d0'], plate: '#41586e', glow: 'rgba(28, 48, 68, 0.5)' },
    { sky: ['#31476f', '#6d4b58'], plate: '#222e42', glow: 'rgba(10, 18, 32, 0.6)' },
    { sky: ['#070c18', '#0d1730'], plate: '#060b12', glow: 'rgba(0, 0, 0, 0.6)' },
  ];
  // Row 0 (day) -> row 2 (night) for the map ground. Every variant keeps the
  // same contrast budget so units, walls and the grid stay legible at night.
  const TERRAIN_PALETTES = [
    {
      ground: ['#c6b184', '#b3a077', '#9e8e6b'],
      speck: 'rgba(255, 252, 240, 1)',
      shade: 'rgba(112, 94, 62, 1)',
      grid: 'rgba(56, 70, 62, 0.14)', grid5: 'rgba(40, 58, 48, 0.24)',
      zone: 'rgba(40, 33, 18, 0.16)',
    },
    {
      ground: ['#7a6a63', '#6a5b58', '#57494a'],
      speck: 'rgba(255, 214, 156, 1)',
      shade: 'rgba(64, 44, 40, 1)',
      grid: 'rgba(226, 196, 158, 0.10)', grid5: 'rgba(240, 206, 160, 0.18)',
      zone: 'rgba(30, 18, 10, 0.18)',
    },
    {
      ground: ['#33465c', '#2c3d52', '#243346'],
      speck: 'rgba(214, 232, 255, 1)',
      shade: 'rgba(10, 20, 34, 1)',
      grid: 'rgba(150, 190, 230, 0.10)', grid5: 'rgba(160, 205, 245, 0.17)',
      zone: 'rgba(4, 10, 20, 0.22)',
    },
  ];

  /** 0 (full daylight) .. 1 (deep night), plus how warm the dusk/dawn is. */
  function lightInfo(phase) {
    const info = phase || {};
    const factor = typeof info.light === 'number'
      ? U.clamp(info.light, 0, 1)
      : (info.isDay === false ? 1 : U.clamp(Number(info.darkness) || 0, 0, 1));
    const warmth = U.clamp(Math.max(Number(info.dusk) || 0, Number(info.dawn) || 0), 0, 1);
    return { factor, warmth };
  }

  function rgbTriple(value) {
    if (Array.isArray(value)) return value.slice(0, 3).map(Number);
    const text = String(value).trim();
    // #rgb / #rrggbb and rgb()/rgba() both parse; anything else is a bug here.
    const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(text);
    if (hex) {
      const raw = hex[1];
      const full = raw.length === 3 ? raw.split('').map((c) => c + c).join('') : raw;
      return [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16));
    }
    const nums = text.match(/\d+(?:\.\d+)?/g);
    if (nums && nums.length >= 3) return nums.slice(0, 3).map(Number);
    throw new Error(`unsupported colour: ${text}`);
  }

  function mixRgb(a, b, t) {
    const from = rgbTriple(a); const to = rgbTriple(b);
    return `rgb(${Math.round(U.lerp(from[0], to[0], t))}, ${Math.round(U.lerp(from[1], to[1], t))}, ${Math.round(U.lerp(from[2], to[2], t))})`;
  }

  function mixHex(a, b, t) {
    const from = U.hexToRgb(a); const to = U.hexToRgb(b);
    return `rgb(${Math.round(U.lerp(from[0], to[0], t))}, ${Math.round(U.lerp(from[1], to[1], t))}, ${Math.round(U.lerp(from[2], to[2], t))})`;
  }

  /**
   * Continuous sky/plate palette. Daylight and night are three stops apart, so
   * the dusk shoulder lands on the warm middle stop instead of jumping.
   */
  function skyPalette(phase) {
    const { factor, warmth } = lightInfo(phase);
    const at = factor * (SKY_PALETTES.length - 1);
    const lo = Math.min(SKY_PALETTES.length - 2, Math.floor(at));
    const t = U.clamp(at - lo, 0, 1);
    const a = SKY_PALETTES[lo];
    const b = SKY_PALETTES[lo + 1];
    // Stops may already be mixed rgb() strings, so mixing stays in RGB triples.
    const stops = a.sky.map((color, i) => mixRgb(color, b.sky[i], t));
    if (warmth > 0) {
      // Dusk and dawn share the warm shoulder; the phase tells them apart in the
      // HUD, the palette only has to read as a low warm sun on the horizon.
      stops[1] = mixRgb(stops[1], '#d98a52', warmth * 0.45);
    }
    return {
      key: `${stops[0]}|${stops[1]}`,
      stops,
      plate: mixHex(a.plate, b.plate, t),
      glow: t < 0.5 ? a.glow : b.glow,
    };
  }

  /** Discrete ground palette; only three variants, so terrain rebuilds stay rare. */
  function terrainPalette(phase) {
    const { factor } = lightInfo(phase);
    const index = factor < 0.34 ? 0 : (factor < 0.7 ? 1 : 2);
    return { index, ...TERRAIN_PALETTES[index] };
  }

  // Default: one small identity label. Details expand only on selection/hover.
  const CALLOUT = {
    width: 152, compactWidth: 84, compactHeight: 22, compactTaskHeight: 28,
    height: 46, taskHeight: 66, pad: 8,
    titleFont: 'bold 14px "Microsoft YaHei", sans-serif',
    infoFont: '12px "Microsoft YaHei", sans-serif',
    taskFont: '12px "Microsoft YaHei", sans-serif',
    titleBaseline: 19, lineAdvance: 15, barHeight: 3, barGap: 2,
    anchorOffset: 16, boxGap: 8,
  };
  const CALLOUT_STYLE = {
    worker: { title: '#a6ffe7', accent: '#72edd0', health: '#edf5ff', low: '#ff9da9', info: '#bcd6e4' },
    pioneer: { title: '#ffe09a', accent: '#f4cf70', health: '#edf5ff', low: '#ff9da9', info: '#cbd6e1' },
  };

  class Renderer {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d');
      this.camera = { x: 0, y: 0, scale: 1 };
      this.viewport = { width: 960, height: 620, dpr: 1 };
      this.options = {
        grid: true,
        ranges: true,
        preview: true,
        executed: true,
        health: true,
        labels: true,
        lighting: true,
      };
      this.tile = BASE_TILE;
      this.hover = null;
      this.selected = null;
      this.hoverCell = null;
      this.selection = new Set();
      this.terrain = null;
      this.terrainKey = '';
      this.terrainCache = new Map();
      this.skyKey = '';
      this.sky = null;
      this.lastSize = { width: 0, height: 0 };
      this._time = 0;
    }

    /**
     * Resize the backing store and return whether it actually changed.
     *
     * Writing canvas.width/height clears the bitmap, and the ResizeObserver can
     * report the same box many times per second; repainting an identical size is
     * what makes the map flash. Callers use the return value to skip a re-fit.
     */
    setSize(width, height) {
      const dpr = Math.min(2, global.devicePixelRatio || 1);
      const pixelWidth = Math.max(1, Math.round(width * dpr));
      const pixelHeight = Math.max(1, Math.round(height * dpr));
      if (this.canvas.width === pixelWidth && this.canvas.height === pixelHeight
        && this.viewport.width === width && this.viewport.height === height
        && this.viewport.dpr === dpr) {
        return false;
      }
      this.viewport = { width, height, dpr };
      this.canvas.width = pixelWidth;
      this.canvas.height = pixelHeight;
      this.canvas.style.width = `${width}px`;
      this.canvas.style.height = `${height}px`;
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.lastSize = { width, height };
      return true;
    }

    mapSize(world) {
      const info = (world.state && world.state.mapInfo) || { width: 41, height: 32 };
      return { width: info.width, height: info.height };
    }

    /**
     * Focus the camera. Cell coordinates of the footprint's top-left corner are
     * converted to map pixels first, so the camera always speaks the same
     * coordinate system draw() uses.
     */
    focusCell(pos, world, span) {
      const map = this.mapSize(world);
      const rect = HW.mapRect(pos, span, map.height, BASE_TILE);
      this.camera.x = rect.cx;
      this.camera.y = rect.cy;
    }

    fit(world, padding) {
      const map = this.mapSize(world);
      const pad = padding == null ? 12 : padding;
      const { width, height } = this.viewport;
      const scaleX = (width - pad * 2) / (map.width * BASE_TILE);
      const scaleY = (height - pad * 2) / (map.height * BASE_TILE);
      // A hair under the exact ratio, so the outermost grid line is never clipped.
      this.camera.scale = U.clamp(Math.min(scaleX, scaleY) * 0.98, MIN_SCALE, MAX_SCALE);
      this.camera.x = (map.width * BASE_TILE) / 2;
      this.camera.y = (map.height * BASE_TILE) / 2;
    }

    /**
     * Centre of a `span`-cell footprint at cell (pos.x, pos.y) -> screen point.
     * Official y grows upwards, so the row is flipped here: this is the only
     * place the flip lives, and every draw/pick path goes through it.
     */
    cellToScreen(pos, world, span) {
      const map = this.mapSize(world);
      const rect = HW.mapRect(pos, span, map.height, BASE_TILE);
      const px = rect.cx;
      const py = rect.cy;
      return {
        x: (px - this.camera.x) * this.camera.scale + this.viewport.width / 2,
        y: (py - this.camera.y) * this.camera.scale + this.viewport.height / 2,
      };
    }

    /** Screen centre of a footprint (alias kept for readability at call sites). */
    cellCenterScreen(pos, world, span) {
      return this.cellToScreen(pos, world, span);
    }

    centerOnActor(world, actor) {
      if (!actor) return;
      this.focusCell(actor.pos, world, actor.footprint || HW.footprint(actor.kind));
    }

    zoomAt(screenX, screenY, factor) {
      const before = this.screenToWorld(screenX, screenY);
      this.camera.scale = U.clamp(this.camera.scale * factor, MIN_SCALE, MAX_SCALE);
      const after = this.screenToWorld(screenX, screenY);
      this.camera.x += before.x - after.x;
      this.camera.y += before.y - after.y;
    }

    panBy(dx, dy) {
      this.camera.x -= dx / this.camera.scale;
      this.camera.y -= dy / this.camera.scale;
    }

    screenToWorld(sx, sy) {
      return {
        x: (sx - this.viewport.width / 2) / this.camera.scale + this.camera.x,
        y: (sy - this.viewport.height / 2) / this.camera.scale + this.camera.y,
      };
    }

    worldToScreen(wx, wy) {
      return {
        x: (wx - this.camera.x) * this.camera.scale + this.viewport.width / 2,
        y: (wy - this.camera.y) * this.camera.scale + this.viewport.height / 2,
      };
    }

    screenToCell(sx, sy, world) {
      const point = this.screenToWorld(sx, sy);
      const map = this.mapSize(world);
      const x = Math.floor(point.x / BASE_TILE);
      const y = map.height - 1 - Math.floor(point.y / BASE_TILE);
      return { x, y };
    }

    /** Kept for callers that only need the centre of a footprint. */
    cellCenter(pos, world, size) {
      return this.cellToScreen(pos, world, size);
    }

    /**
     * Actor under a screen point. Buildings win over the single cells they
     * overlap (clicking a base should select the base, not a worker standing
     * next to it), then the closest centre wins.
     */
    pick(world, sx, sy) {
      const candidates = [];
      for (const actor of world.actors) {
        const center = this.cellToScreen(actor.pos, world, actor.footprint || HW.footprint(actor.kind));
        const shape = actor.footprint || HW.footprint(actor.kind);
        const halfX = BASE_TILE * this.camera.scale * shape.width / 2;
        const halfY = BASE_TILE * this.camera.scale * shape.height / 2;
        const dx = Math.abs(sx - center.x);
        const dy = Math.abs(sy - center.y);
        if (dx > halfX || dy > halfY) continue;
        candidates.push({
          actor,
          score: Math.max(dx / halfX, dy / halfY),
          big: actor.size > 1 ? 1 : 0,
        });
      }
      if (!candidates.length) return null;
      candidates.sort((a, b) => (b.big - a.big) || (a.score - b.score));
      return candidates[0].actor;
    }

    zoneAt(world, cell) {
      return world.zones.find((zone) => {
        const shape = zone.footprint || HW.footprint(zone.kind);
        return cell.x >= zone.pos.x && cell.x < zone.pos.x + shape.width
          && cell.y <= zone.pos.y && cell.y > zone.pos.y - shape.height;
      }) || null;
    }

    /* ------------------------------------------------------------ terrain */

    /**
     * The ground bitmap for one day/dusk/night palette. It is cached per
     * (size, variant), so a phase flip or a replay seek rebuilds at most once
     * and the steady state never rebuilds terrain while drawing frames.
     */
    buildTerrain(world, phase, record) {
      const map = this.mapSize(world);
      const palette = terrainPalette(phase);
      const key = `${map.width}x${map.height}:${palette.index}`;
      if (this.terrainCache.has(key)) {
        this.terrain = this.terrainCache.get(key);
        this.terrainKey = key;
        return;
      }
      const canvas = document.createElement('canvas');
      canvas.width = map.width * BASE_TILE;
      canvas.height = map.height * BASE_TILE;
      const ctx = canvas.getContext('2d');
      const pick = record || (() => true);
      const gradient = ctx.createLinearGradient(0, 0, canvas.width, canvas.height);
      const stops = [[0, palette.ground[0]], [0.5, palette.ground[1]], [1, palette.ground[2]]];
      for (const [at, color] of stops) gradient.addColorStop(at, color);
      if (pick('fill')) ctx.fillStyle = gradient;
      if (pick('fill')) ctx.fillRect(0, 0, canvas.width, canvas.height);
      // Deterministic gravel so the ground reads as a real surface; the speck
      // colour follows the palette, keeping daylight bright and night cool.
      for (let gy = 0; gy < map.height; gy += 1) {
        for (let gx = 0; gx < map.width; gx += 1) {
          const noise = hash2(gx, gy);
          if (noise > 0.66) {
            const alpha = palette.index === 0 ? 0.10 + noise * 0.12 : 0.03 + noise * 0.07;
            if (pick('speck')) ctx.fillStyle = U.rgba(palette.speck, alpha);
            if (pick('speck')) ctx.fillRect(gx * BASE_TILE + noise * 18, gy * BASE_TILE + hash2(gy, gx) * 18, 3, 3);
          }
          if (noise < 0.05) {
            if (pick('shade')) ctx.fillStyle = U.rgba(palette.shade, 0.10);
            if (pick('shade')) ctx.fillRect(gx * BASE_TILE, gy * BASE_TILE, BASE_TILE, BASE_TILE);
          }
        }
      }
      this.terrainCache.set(key, canvas);
      this.terrain = canvas;
      this.terrainKey = key;
    }

    /* --------------------------------------------------------------- draw */

    draw(world, effects, ui, timestamp, record) {
      this._time = timestamp || performance.now();
      const ctx = this.ctx;
      const { width, height } = this.viewport;
      const map = this.mapSize(world);
      const tile = BASE_TILE;
      const phase = world.phase || HW.phaseInfo(world.roundNo);
      const sky = skyPalette(phase);
      this.buildTerrain(world, phase, record);

      ctx.save();
      ctx.clearRect(0, 0, width, height);
      if (this.skyKey !== sky.key || !this.sky) {
        const gradient = ctx.createLinearGradient(0, 0, 0, height);
        gradient.addColorStop(0, sky.stops[0]);
        gradient.addColorStop(1, sky.stops[1]);
        this.sky = gradient;
        this.skyKey = sky.key;
      }
      ctx.fillStyle = this.sky;
      ctx.fillRect(0, 0, width, height);
      ctx.restore();

      const scale = this.camera.scale;
      // Map pixels already use screen orientation (y down, row 0 on top), so the
      // canvas transform starts at the top-left corner of the playfield.
      const origin = this.worldToScreen(0, 0);

      ctx.save();
      ctx.translate(origin.x, origin.y);
      ctx.scale(scale, scale);

      // Playfield plate. It frames the same surface the terrain uses, so a
      // daylight map is not wrapped in a night-black border.
      ctx.save();
      ctx.shadowColor = sky.glow;
      ctx.shadowBlur = 26;
      ctx.fillStyle = sky.plate;
      ctx.fillRect(-6, -6, map.width * tile + 12, map.height * tile + 12);
      ctx.restore();
      ctx.drawImage(this.terrain, 0, 0);

      this.drawGrid(ctx, map, tile, phase);

      for (const zone of world.zones) {
        const rect = HW.mapRect(zone.pos, zone.footprint || HW.footprint(zone.kind), map.height, tile);
        S.drawZone(ctx, rect.cx, rect.cy, tile, zone);
        if (this.selectedZone && this.selectedZone.kind === zone.kind
            && this.selectedZone.pos.x === zone.pos.x && this.selectedZone.pos.y === zone.pos.y) {
          this.drawSelection(ctx, zone, rect.cx, rect.cy, tile, zone.size);
        }
      }

      // Depth sort: buildings claim more space, so they sort by their far edge.
      const order = world.actors.slice().sort((a, b) => {
        const da = a.pos.y - HW.footprint(a.kind).height + 1;
        const db = b.pos.y - HW.footprint(b.kind).height + 1;
        if (da !== db) return db - da;
        return a.pos.x - b.pos.x;
      });

      this.drawTrails(ctx, world, ui, tile, map);
      for (const actor of order) this.drawActor(ctx, world, actor, tile, map, ui);
      this.drawDeaths(ctx, world, tile, map, ui);
      if (effects) effects.draw(ctx, this._time);

      this.drawOverlays(ctx, world, ui, tile, map);

      ctx.restore();
      this.drawLighting(ctx, world, map, tile);
      this.drawScreenLabels(ctx, world, ui);
    }

    drawScreenLabels(ctx, world, ui) {
      if (!this.options.labels) return;
      // Buildings and market sprites already carry their visual identity. Keep
      // the map readable by reserving text nameplates for task/resource points;
      // the base, vendor and weapon shop do not need duplicate names.
      const sites = world.zones.filter((z) => !['stone', 'iron', 'copper', 'vendor', 'weaponShop'].includes(z.kind));
      const boxes = this.drawCrewLabels(ctx, world, ui);
      ctx.save(); ctx.font = '14px "Microsoft YaHei", sans-serif'; ctx.textAlign = 'center';
      for (const site of sites) {
        const point = this.cellToScreen(site.pos, world, site.footprint || HW.footprint(site.kind));
        const width = ctx.measureText(site.label).width + 14;
        const x = point.x - width / 2, y = point.y + 14;
        if (x < 0 || x + width > this.viewport.width || y < 0 || y + 22 > this.viewport.height) continue;
        if (boxes.some((b) => x < b.x + b.w && x + width > b.x && y < b.y + b.h && y + 22 > b.y)) continue;
        boxes.push({x,y,w:width,h:22});
        ctx.fillStyle = '#06121eea'; ctx.fillRect(x, y, width, 22);
        ctx.fillStyle = site.owner === 'own' ? '#88f5d0' : site.owner === 'enemy' ? '#ffb2bc' : '#e3edf8';
        ctx.fillText(site.label, point.x, y + 15);
      }
      ctx.restore();
    }

    /**
     * Compact nameplates for the controllable crew: identity + ID on line 1,
     * health and backpack count on line 2, plus a short task countdown and thin
     * bar while a task is active. They stay in screen space so the text keeps a
     * readable CSS-pixel size at every zoom, and their anchors use the same
     * committed walk interpolation as the sprites.
     *
     * Exact current/max health and the itemised backpack stay in the console
     * cards; this overlay only carries what has to be visible next to the unit.
     */
    drawCrewLabels(ctx, world, ui) {
      const boxes = [];
      const crew = world.actors.filter((a) => (['own', 'enemy'].includes(a.owner) && a.kind === 'worker')
          || (a.owner === 'own' && a.kind === 'pioneer'))
        .sort((a, b) => String(a.id).localeCompare(String(b.id)));
      const workerNames = new Map();
      for (const owner of ['own', 'enemy']) {
        const field = owner === 'own' ? 'teamOur' : 'teamEnemy';
        const team = (world.state || {})[field] || {};
        const ourSide = ((world.state || {}).teamOur || {}).type || world.side || 'challenger';
        const side = team.type || (owner === 'own' ? ourSide
          : ourSide === 'challenger' ? 'defender' : 'challenger');
        // Include dead/initial roster members so the surviving second worker
        // does not get renamed as the first. IDs identify roles, not teams.
        const ids = new Set();
        for (const state of [world.states && world.states[0], world.state]) {
          for (const role of ((state || {})[field] || {}).roles || []) {
            if (role.roleType === 'worker') ids.add(String(role.id));
          }
        }
        for (const actor of crew) if (actor.owner === owner && actor.kind === 'worker') ids.add(String(actor.id));
        [...ids].sort((a, b) => a.localeCompare(b, undefined, {numeric: true})).forEach((id, i) => {
          workerNames.set(`${owner}:${id}`, `${side === 'challenger' ? '蓝' : '红'}${['一', '二'][i] || i + 1}`);
        });
      }
      const task = HW.viewModel.taskStatus(world);
      ctx.save();
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
      for (const actor of crew) {
        const walk = actor.anim && actor.anim.find((a) => a.type === 'walk');
        const t = walk && ui && ui.walkProgress != null ? U.easeOut(U.clamp(ui.walkProgress, 0, 1)) : 1;
        const pos = walk ? {x: U.lerp(walk.from.x, walk.to.x, t), y: U.lerp(walk.from.y, walk.to.y, t)} : actor.rpos;
        const point = this.cellToScreen(pos, world, actor.footprint || HW.footprint(actor.kind));
        if (point.x < 0 || point.x > this.viewport.width || point.y < 0 || point.y > this.viewport.height) continue;
        if (actor.kind === 'worker') {
          const title = workerNames.get(`${actor.owner}:${actor.id}`);
          ctx.font = 'bold 14px "Microsoft YaHei", sans-serif';
          ctx.fillStyle = title.startsWith('蓝') ? '#2389ee' : '#ed5265';
          const width = ctx.measureText(title).width;
          const x = U.clamp(point.x - width / 2, 4, Math.max(4, this.viewport.width - width - 4));
          const y = Math.max(18, point.y - BASE_TILE * this.camera.scale / 2 - 4);
          ctx.fillText(title, x, y);
          continue; // Plain text only; details remain in the console.
        }
        const style = CALLOUT_STYLE[actor.kind] || CALLOUT_STYLE.worker;
        const onTask = actor.kind === 'pioneer' && task.active;
        const maxHealth = Number(actor.maxHealth) || 1;
        const ratio = U.clamp((Number(actor.health) || 0) / maxHealth, 0, 1);
        const same = (other) => other && other.id === actor.id && other.owner === actor.owner;
        const detailed = actor.selected || same(this.selected) || same(ui && ui.hoverActor);
        const title = '拓';

        ctx.font = CALLOUT.titleFont;
        // Compact by default; only the focused role gets a full plate.
        const w = detailed ? CALLOUT.width : CALLOUT.compactWidth;
        const h = detailed ? (onTask ? CALLOUT.taskHeight : CALLOUT.height) : (onTask ? CALLOUT.compactTaskHeight : CALLOUT.compactHeight);
        const pad = 6;
        const clampBox = (x, y) => ({x: U.clamp(x, pad, Math.max(pad, this.viewport.width - w - pad)),
          y: U.clamp(y, 58, Math.max(58, this.viewport.height - h - 40)), w, h});
        const candidates = [];
        for (let row = 0; row < 5; row += 1) {
          for (const x of [point.x + CALLOUT.anchorOffset, point.x - w - CALLOUT.anchorOffset]) {
            candidates.push(clampBox(x, point.y - h - CALLOUT.anchorOffset - row * (h + CALLOUT.boxGap)));
            candidates.push(clampBox(x, point.y + CALLOUT.anchorOffset + row * (h + CALLOUT.boxGap)));
          }
        }
        const overlaps = (a, b) => a.x < b.x + b.w + 6 && a.x + a.w + 6 > b.x && a.y < b.y + b.h + 6 && a.y + a.h + 6 > b.y;
        // Prefer empty map space instead of covering adjacent guns/walls/crew.
        const occupied = world.actors.map((unit) => {
          const at = this.cellToScreen(unit.rpos || unit.pos, world, unit.footprint || HW.footprint(unit.kind));
          const span = BASE_TILE * this.camera.scale * (unit.size || 1);
          return {x:at.x-span/2, y:at.y-span/2, w:span, h:span};
        });
        const candidatesFree = candidates.filter(c => !boxes.some(b => overlaps(c,b)));
        const choices = candidatesFree.length ? candidatesFree : candidates;
        const box = choices.find(c => !occupied.some(b => overlaps(c,b))) || choices[0];
        boxes.push(box);
        const {x, y} = box;
        ctx.strokeStyle = style.accent;
        ctx.lineWidth = 1.5;
        if (detailed) {
          ctx.beginPath(); ctx.moveTo(point.x, point.y);
          ctx.lineTo(U.clamp(point.x, x, x + w), U.clamp(point.y, y, y + h)); ctx.stroke();
        }
        ctx.fillStyle = '#081725f2';
        P.roundRect(ctx, x, y, w, h, 7); ctx.fill(); ctx.stroke();

        if (!detailed) {
          ctx.font = 'bold 12px "Microsoft YaHei", sans-serif';
          ctx.fillStyle = ratio <= 0.25 ? style.low : style.title;
          ctx.fillText(this.truncate(ctx, title, w - 12), x + 6, y + 15);
          ctx.fillStyle = '#324358'; ctx.fillRect(x + 4, y + 19, w - 8, 2);
          ctx.fillStyle = ratio <= 0.25 ? style.low : style.accent;
          ctx.fillRect(x + 4, y + 19, (w - 8) * ratio, 2);
          if (onTask) {
            ctx.fillStyle = '#324358'; ctx.fillRect(x + 4, y + 24, w - 8, 3);
            if (task.ratio != null) {
              ctx.fillStyle = '#f4cf70'; ctx.fillRect(x + 4, y + 24, (w - 8) * task.ratio, 3);
            }
          }
          continue;
        }
        const budget = w - CALLOUT.pad * 2;
        const barY = y + h - CALLOUT.barGap - CALLOUT.barHeight;
        let cursorY = y + CALLOUT.titleBaseline;
        ctx.font = CALLOUT.titleFont;
        ctx.fillStyle = style.title;
        ctx.fillText(this.truncate(ctx, title, budget), x + CALLOUT.pad, cursorY);
        cursorY += CALLOUT.lineAdvance;
        ctx.font = CALLOUT.infoFont;
        ctx.fillStyle = ratio > 0.25 ? style.health : style.low;
        const hp = `${actor.health}/${actor.maxHealth}`;
        const bag = `${actor.backpack.length}/${actor.capacity || '—'}`;
        // Concise line, full numbers: "HP 220/220 | 包 2/100". The space around
        // the separator is dropped before anything else, and the long health
        // label only shrinks if a very large capacity still would not fit;
        // itemised contents stay in the console crew card.
        const forms = [`生命 ${hp} | 背包 ${bag}`, `HP ${hp} | 背包 ${bag}`,
          `HP ${hp} |包 ${bag}`, `HP ${hp}|包 ${bag}`];
        let info = forms[0];
        for (const form of forms) {
          info = form;
          if (ctx.measureText(form).width <= budget) break;
        }
        ctx.fillText(info, x + CALLOUT.pad, cursorY);
        if (onTask) {
          cursorY += CALLOUT.lineAdvance;
          ctx.font = CALLOUT.taskFont;
          ctx.fillStyle = '#ffe09a';
          ctx.fillText(this.truncate(ctx, task.meter, budget), x + CALLOUT.pad, cursorY);
          // The thin bar sits just under the last text baseline and inside the plate.
          ctx.fillStyle = '#324358';
          ctx.fillRect(x + CALLOUT.pad, barY, budget, CALLOUT.barHeight);
          if (task.ratio != null) {
            ctx.fillStyle = '#f4cf70';
            ctx.fillRect(x + CALLOUT.pad, barY, budget * U.clamp(task.ratio, 0, 1), CALLOUT.barHeight);
          }
        }
      }
      ctx.restore();
      return boxes;
    }

    /** Trim a string with an ellipsis so it fits `width` in the current font. */
    truncate(ctx, text, width) {
      const value = String(text == null ? '' : text);
      if (width <= 0) return '';
      if (ctx.measureText(value).width <= width) return value;
      let cut = value.length;
      while (cut > 1 && ctx.measureText(`${value.slice(0, cut)}…`).width > width) cut -= 1;
      return `${value.slice(0, cut)}…`;
    }

    /** Grid ink follows the phase palette so lines stay visible on light ground. */
    drawGrid(ctx, map, tile, phase) {
      if (!this.options.grid) return;
      const palette = terrainPalette(phase);
      ctx.save();
      ctx.lineWidth = 1 / this.camera.scale;
      for (let x = 0; x <= map.width; x += 1) {
        ctx.strokeStyle = x % 5 === 0 ? palette.grid5 : palette.grid;
        ctx.beginPath();
        ctx.moveTo(x * tile, 0);
        ctx.lineTo(x * tile, map.height * tile);
        ctx.stroke();
      }
      for (let y = 0; y <= map.height; y += 1) {
        ctx.strokeStyle = y % 5 === 0 ? palette.grid5 : palette.grid;
        ctx.beginPath();
        ctx.moveTo(0, y * tile);
        ctx.lineTo(map.width * tile, y * tile);
        ctx.stroke();
      }
      ctx.restore();
    }

    drawActor(ctx, world, actor, tile, map, ui) {
      const walk = actor.anim && actor.anim.find((a) => a.type === 'walk');
      let rx = actor.rpos.x;
      let ry = actor.rpos.y;
      if (walk && ui && ui.walkProgress != null) {
        const t = U.easeOut(U.clamp(ui.walkProgress, 0, 1));
        rx = U.lerp(walk.from.x, walk.to.x, t);
        ry = U.lerp(walk.from.y, walk.to.y, t);
      }
      const span = actor.size;
      const rect = HW.mapRect({x: rx, y: ry}, actor.footprint || HW.footprint(actor.kind), map.height, tile);
      const centerX = rect.cx;
      const centerY = rect.cy;

      ctx.save();
      // Soft halo: separates a unit from dark terrain without inventing detail.
      P.glowSpot(ctx, centerX, centerY, tile * (span === 2 ? 1.5 : 0.86), actor.color.main, 0.14);
      S.paint(ctx, actor, centerX, centerY, tile);
      ctx.restore();

      if (actor.kind === 'wall' || HW.OFFICIAL.towerTypes.includes(actor.kind)) {
        this.drawLevel(ctx, actor, centerX, centerY, tile);
      }
      if (this.options.health) this.drawHealth(ctx, actor, centerX, centerY, tile, span);
      if (this.options.labels) this.drawTeamMark(ctx, actor, centerX, centerY, tile, span);
      if (actor.selected || (this.selected && this.selected.key === actor.key)) {
        this.drawSelection(ctx, actor, centerX, centerY, tile, span);
      }
    }

    drawLevel(ctx, actor, x, y, tile) {
      const level = Math.max(1, Math.min(3, Number(actor.level) || 1));
      const colors = ['#c6d2e0', '#67e8d1', '#ffd166'];
      // Keep text at least 10 screen pixels at full-map zoom; no large nameplate.
      const font = Math.max(10 / this.camera.scale, tile * 0.34);
      const label = String(level);
      ctx.save();
      ctx.font = `bold ${font}px ui-monospace, monospace`;
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      const bx = x + tile * 0.25, by = y + tile * 0.23;
      ctx.fillStyle = '#101b2a';
      ctx.fillRect(bx - font * 0.54, by - font * 0.58, font * 1.08, font * 1.16);
      ctx.strokeStyle = colors[level - 1]; ctx.lineWidth = 1 / this.camera.scale;
      ctx.strokeRect(bx - font * 0.54, by - font * 0.58, font * 1.08, font * 1.16);
      ctx.fillStyle = colors[level - 1]; ctx.fillText(label, bx, by);
      ctx.restore();
    }

    drawHealth(ctx, actor, x, y, tile, span) {
      if (actor.kind === 'wall' && actor.health >= actor.maxHealth) return;
      if (actor.owner !== 'own' && actor.owner !== 'robot' && actor.kind === 'wall' && actor.health > 0) return;
      const ratio = U.clamp(actor.health / (actor.maxHealth || 1), 0, 1);
      if (ratio >= 1 && actor.kind !== 'station') return;
      const width = tile * (span === 2 ? 1.5 : 0.72);
      const height = Math.max(3.4, tile * 0.11);
      const bx = x - width / 2;
      const by = y - tile * (span === 2 ? 0.95 : 0.62) - height;
      ctx.save();
      ctx.fillStyle = 'rgba(6, 10, 16, 0.82)';
      ctx.fillRect(bx - 1, by - 1, width + 2, height + 2);
      const color = ratio > 0.55 ? PALETTE.hp : (ratio > 0.25 ? PALETTE.hpWarn : PALETTE.hpBad);
      ctx.fillStyle = color;
      ctx.fillRect(bx, by, width * ratio, height);
      if (actor.owner === 'robot') {
        ctx.strokeStyle = U.rgba(PALETTE.robot, 0.85);
        ctx.lineWidth = 1;
        ctx.strokeRect(bx - 1.5, by - 1.5, width + 3, height + 3);
      }
      ctx.restore();
    }

    drawTeamMark(ctx, actor, x, y, tile, span) {
      const theme = actor.color;
      if (actor.owner === 'own' && actor.kind !== 'station') {
        ctx.save();
        ctx.fillStyle = U.rgba(theme.main, 0.95);
        P.poly(ctx, [[x - 3.6, y + tile * 0.52], [x + 3.6, y + tile * 0.52], [x, y + tile * 0.52 + 5]], theme.main);
        ctx.fill();
        ctx.restore();
      } else if (actor.owner === 'enemy') {
        ctx.save();
        ctx.strokeStyle = U.rgba(theme.main, 0.9);
        ctx.lineWidth = 1.4;
        ctx.setLineDash([3, 3]);
        ctx.strokeRect(x - tile * 0.46 * span, y - tile * 0.42 * span, tile * 0.92 * span, tile * 0.86 * span);
        ctx.setLineDash([]);
        ctx.restore();
      }
      if (actor.capacity && actor.backpack && actor.backpack.length) {
        const ratio = actor.backpack.length / actor.capacity;
        ctx.save();
        ctx.fillStyle = 'rgba(6, 10, 16, 0.8)';
        ctx.fillRect(x - 9, y + tile * 0.4, 18, 3);
        ctx.fillStyle = ratio > 0.85 ? PALETTE.hpBad : PALETTE.shield;
        ctx.fillRect(x - 9, y + tile * 0.4, 18 * ratio, 3);
        ctx.restore();
      }
    }

    drawSelection(ctx, actor, x, y, tile, span) {
      const pulse = 0.6 + 0.4 * Math.sin(this._time / 220);
      const shape = actor.footprint || HW.footprint(actor.kind);
      const halfX = tile * shape.width / 2;
      const halfY = tile * shape.height / 2;
      ctx.save();
      ctx.strokeStyle = U.rgba(PALETTE.hot, 0.55 + pulse * 0.45);
      ctx.lineWidth = 2;
      ctx.setLineDash([6, 4]);
      ctx.lineDashOffset = -this._time / 60;
      ctx.strokeRect(x - halfX, y - halfY, halfX * 2, halfY * 2);
      ctx.setLineDash([]);
      ctx.lineWidth = 2.4;
      const bracket = Math.min(halfX, halfY) * 0.42;
      for (const [sx, sy] of [[-1, -1], [1, -1], [-1, 1], [1, 1]]) {
        ctx.beginPath();
        ctx.moveTo(x + sx * halfX, y + sy * halfY - sy * bracket);
        ctx.lineTo(x + sx * halfX, y + sy * halfY);
        ctx.lineTo(x + sx * halfX - sx * bracket, y + sy * halfY);
        ctx.stroke();
      }
      ctx.restore();
    }

    drawTrails(ctx, world, ui, tile, map) {
      if (!ui || !ui.trails || !ui.trails.length) return;
      ctx.save();
      ctx.strokeStyle = 'rgba(126, 240, 208, 0.45)';
      ctx.lineWidth = 2 / this.camera.scale + 1;
      ctx.setLineDash([4, 4]);
      for (const trail of ui.trails) {
        const from = { x: trail.from.x * tile + tile / 2, y: (map.height - 1 - trail.from.y) * tile + tile / 2 };
        const to = { x: trail.to.x * tile + tile / 2, y: (map.height - 1 - trail.to.y) * tile + tile / 2 };
        ctx.beginPath();
        ctx.moveTo(from.x, from.y);
        ctx.lineTo(to.x, to.y);
        ctx.stroke();
      }
      ctx.restore();
    }

    drawDeaths(ctx, world, tile, map, ui) {
      if (!world.deaths || !ui || ui.walkProgress == null) return;
      const t = U.clamp(ui.walkProgress, 0, 1);
      if (t > 0.85) return;
      ctx.save();
      for (const death of world.deaths) {
        const ghost = Object.assign({}, death, {
          color: HW.OWNER_COLORS[death.owner] || HW.OWNER_COLORS.neutral,
          level: death.level || 1, anim: [], hitFlash: 0, size: 1, rpos: death.pos,
          maxHealth: 1, health: 1, backpack: [], capacity: 0,
        });
        const rect = HW.mapRect(death.pos, HW.footprint(death.kind), map.height, tile);
        const x = rect.cx, y = rect.cy;
        ctx.save();
        ctx.globalAlpha = U.clamp(1 - t / 0.85, 0, 1) * 0.75;
        S.paint(ctx, ghost, x, y, tile);
        ctx.restore();
      }
      ctx.restore();
    }
    drawOverlays(ctx, world, ui, tile, map) {
      const overlay = ui || {};
      if (overlay.hoverActor) {
        const actor = overlay.hoverActor;
        const rect = HW.mapRect(actor.pos, actor.footprint || HW.footprint(actor.kind), map.height, tile);
        ctx.save();
        ctx.strokeStyle = 'rgba(255, 243, 196, 0.55)';
        ctx.lineWidth = 1.4 / this.camera.scale + 0.6;
        ctx.strokeRect(rect.x + 1, rect.y + 1, rect.width - 2, rect.height - 2);
        ctx.restore();
      }
      if (this.options.ranges) {
        for (const actor of world.actors) {
          if (!HW.OFFICIAL.towerTypes.includes(actor.kind)) continue;
          if (actor.owner !== 'own') continue;
          const showAll = overlay.showAllRanges;
          const isSelected = this.selected && this.selected.key === actor.key;
          if (!showAll && !isSelected) continue;
          const cells = Math.min(HW.rangeOf(actor.kind, actor.level), 41);
          ctx.save();
          ctx.strokeStyle = actor.kind === 'rocket' ? 'rgba(255, 179, 71, 0.75)'
            : (actor.kind === 'railgun' ? 'rgba(127, 212, 255, 0.75)' : 'rgba(255, 209, 102, 0.75)');
          ctx.fillStyle = actor.kind === 'rocket' ? 'rgba(255, 179, 71, 0.07)'
            : (actor.kind === 'railgun' ? 'rgba(127, 212, 255, 0.07)' : 'rgba(255, 209, 102, 0.07)');
          ctx.lineWidth = 1.4 / this.camera.scale + 0.5;
          ctx.setLineDash([5, 4]);
          const x0 = Math.max(0, actor.pos.x - cells) * tile;
          const x1 = Math.min(map.width, actor.pos.x + cells + 1) * tile;
          const top = (map.height - Math.min(map.height, actor.pos.y + cells + 1)) * tile;
          const bottom = (map.height - Math.max(0, actor.pos.y - cells)) * tile;
          ctx.fillRect(x0, top, x1 - x0, bottom - top);
          ctx.strokeRect(x0, top, x1 - x0, bottom - top);
          ctx.setLineDash([]);
          ctx.restore();
        }
      }
      if (this.options.preview) this.drawCommands(ctx, world, overlay.previewCommands, tile, map, '#ffd166', true);
      if (this.options.executed) this.drawCommands(ctx, world, overlay.executedCommands, tile, map, '#63e6be', false);
    }

    drawCommands(ctx, world, commands, tile, map, color, dashed) {
      if (!commands) return;
      const entries = Array.isArray(commands) ? commands : Object.entries(commands);
      ctx.save();
      for (const entry of entries) {
        const command = Array.isArray(entry) ? entry[1] : entry.command;
        const executor = Array.isArray(entry) ? entry[0] : entry.executor;
        const action = command.action;
        if (action !== 'move' && action !== 'attack') continue;
        const targets = command.targetPos || [];
        if (!targets.length) continue;
        let origin = command.from || null;
        if (!origin && executor) {
          const action = !dashed && world.frame && (world.frame.actions || []).find((a) => String(a.id) === String(executor));
          const actor = world.actors.find((a) => String(a.id) === String(executor));
          origin = (action && action.from) || (actor && actor.pos) || null;
        }
        if (!origin) {
          const owner = world.actors.find((a) => String(a.id) === String(command.controllerId));
          if (owner) origin = owner.pos;
        }
        if (!origin) continue;
        const from = { x: origin.x * tile + tile / 2, y: (map.height - 1 - origin.y) * tile + tile / 2 };
        const isAttack = action === 'attack';
        const strokeColor = isAttack ? color : (dashed ? color : '#63e6be');
        ctx.strokeStyle = strokeColor;
        ctx.fillStyle = strokeColor;
        ctx.lineWidth = 2 / this.camera.scale + 0.8;
        if (dashed) ctx.setLineDash([7, 5]);
        else ctx.setLineDash([]);
        targets.forEach((target, index) => {
          const to = { x: target.x * tile + tile / 2, y: (map.height - 1 - target.y) * tile + tile / 2 };
          // Attack overlays mark aim points only. A permanent command line
          // looked like a laser for every weapon and hid the actual shot FX.
          if (!isAttack) {
            ctx.beginPath(); ctx.moveTo(from.x, from.y);
            ctx.lineTo(to.x, to.y); ctx.stroke();
          }
          if (isAttack) {
            ctx.beginPath();
            ctx.arc(to.x, to.y, tile * 0.42, 0, Math.PI * 2);
            ctx.stroke();
            ctx.font = `${Math.max(9, tile * 0.3)}px ui-monospace, monospace`;
            ctx.textAlign = 'center';
            ctx.fillText(String(index + 1), to.x, to.y - tile * 0.5);
          } else {
            // Arrow head at the destination cell.
            const angle = Math.atan2(to.y - from.y, to.x - from.x);
            P.poly(ctx, [
              [to.x, to.y],
              [to.x - Math.cos(angle - 0.4) * tile * 0.42, to.y - Math.sin(angle - 0.4) * tile * 0.42],
              [to.x - Math.cos(angle + 0.4) * tile * 0.42, to.y - Math.sin(angle + 0.4) * tile * 0.42],
            ], strokeColor);
            ctx.fill();
          }
        });
      }
      ctx.restore();
    }

    /**
     * Night veil on top of the (already phase-tinted) map. It is a no-op in
     * daylight, so the layer toggle keeps its meaning: turn it off and the night
     * is no longer darkened, but the day/night ground palette stays.
     */
    drawLighting(ctx, world, map, tile) {
      if (!this.options.lighting) return;
      const phase = world.phase || HW.phaseInfo(world.roundNo);
      if (phase.darkness <= 0.02) return;
      const { width, height } = this.viewport;
      const d = phase.darkness;
      ctx.save();
      // Two thin, additive layers instead of one heavy multiply: the night has
      // to stay readable while units and structures remain identifiable.
      const tint = phase.dawn > 0 ? PALETTE.dawn : (phase.dusk > 0 ? PALETTE.dusk : PALETTE.night);
      ctx.globalAlpha = 0.10 + d * 0.12;
      ctx.fillStyle = tint;
      ctx.fillRect(0, 0, width, height);
      ctx.globalAlpha = d * 0.26;
      const gradient = ctx.createLinearGradient(0, 0, 0, height);
      gradient.addColorStop(0, 'rgba(4, 8, 20, 1)');
      gradient.addColorStop(1, 'rgba(10, 18, 34, 0.4)');
      ctx.fillStyle = gradient;
      ctx.fillRect(0, 0, width, height);

      // Base floodlights are art, not vision: they never reveal hidden units,
      // they only brighten cells that are already being drawn.
      if (d > 0.4) {
        ctx.globalCompositeOperation = 'lighter';
        for (const actor of world.actors) {
          if (actor.kind !== 'station' && !HW.OFFICIAL.towerTypes.includes(actor.kind)) continue;
          const screen = this.cellCenterScreen(actor.pos, world, actor.footprint || HW.footprint(actor.kind));
          const radius = (actor.kind === 'station' ? 5.2 : 2.6) * tile * this.camera.scale * (0.55 + d * 0.5);
          const light = ctx.createRadialGradient(screen.x, screen.y, 0, screen.x, screen.y, radius);
          const lightColor = actor.owner === 'enemy' ? '255, 150, 130' : '150, 235, 210';
          light.addColorStop(0, `rgba(${lightColor}, ${0.2 * d})`);
          light.addColorStop(1, `rgba(${lightColor}, 0)`);
          ctx.fillStyle = light;
          ctx.beginPath();
          ctx.arc(screen.x, screen.y, radius, 0, Math.PI * 2);
          ctx.fill();
        }
        ctx.globalCompositeOperation = 'source-over';
      }
      if (d > 0.8) {
        // Stars only in deep night, drawn above the tint.
        ctx.globalAlpha = U.clamp((d - 0.8) * 4, 0, 0.8);
        ctx.fillStyle = 'rgba(220, 235, 255, 0.9)';
        for (let i = 0; i < 48; i += 1) {
          const sx = Math.min(width - 2, Math.max(0, this.cellToScreen({ x: 0, y: 0 }, world, 1).x
            + (0.02 + 0.96 * hash2(i, 3)) * map.width * tile * this.camera.scale));
          const sy = Math.min(height - 2, Math.max(0, (0.02 + 0.6 * hash2(7, i)) * height));
          const twinkle = 0.5 + 0.5 * Math.abs(Math.sin(this._time / 900 + i));
          ctx.fillRect(sx, sy, 1.8 * twinkle, 1.8 * twinkle);
        }
      }
      ctx.restore();
    }
  }

  HW.Renderer = Renderer;
  HW.BASE_TILE = BASE_TILE;
}(window));
