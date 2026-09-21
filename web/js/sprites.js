/* Sprite library: one painter per unit type, plus the primitives they share.
 *
 * Adding a unit type means adding one painter here and one entry in
 * KIND_NAMES/constants.js — no other file needs to change. Unknown kinds fall
 * back to painterUnknown, which draws a neutral marker with the raw type name
 * so a new official field degrades visibly instead of breaking the frame.
 *
 * These are top-down views of the official grid cells. Buildings are drawn from
 * the cell's bottom edge upward so overlapping structures still read correctly.
 */
(function (global) {
  'use strict';

  const HW = global.HW = global.HW || {};
  const U = HW.util;
  const PALETTE = HW.PALETTE;

  const TAU = Math.PI * 2;

  /* ------------------------------------------------------------ primitives */

  function poly(ctx, points) {
    ctx.beginPath();
    points.forEach((p, index) => (index ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
    ctx.closePath();
  }

  function roundRect(ctx, x, y, w, h, r) {
    const radius = Math.min(r, Math.abs(w) / 2, Math.abs(h) / 2);
    ctx.beginPath();
    ctx.moveTo(x + radius, y);
    ctx.arcTo(x + w, y, x + w, y + h, radius);
    ctx.arcTo(x + w, y + h, x, y + h, radius);
    ctx.arcTo(x, y + h, x, y, radius);
    ctx.arcTo(x, y, x + w, y, radius);
    ctx.closePath();
  }

  function fillPoly(ctx, points, fillStyle) {
    poly(ctx, points);
    ctx.fillStyle = fillStyle;
    ctx.fill();
  }

  /** Soft ground shadow under a footprint. */
  function shadow(ctx, x, y, size, height, strength) {
    ctx.save();
    ctx.globalAlpha = strength == null ? 0.42 : strength;
    const gradient = ctx.createRadialGradient(
      x + size * 0.08, y + size * 0.12, size * 0.1,
      x + size * 0.08, y + size * 0.12, size * 1.05,
    );
    gradient.addColorStop(0, 'rgba(0, 0, 0, 0.75)');
    gradient.addColorStop(1, 'rgba(0, 0, 0, 0)');
    ctx.fillStyle = gradient;
    ctx.beginPath();
    ctx.ellipse(x + size * 0.1, y + size * 0.14, size * 0.62, size * 0.4, 0, 0, TAU);
    ctx.fill();
    ctx.restore();
    if (height) {
      ctx.save();
      ctx.globalAlpha = 0.22;
      ctx.fillStyle = '#000';
      ctx.fillRect(x - size * 0.42, y - height * 0.1, size * 0.5, height * 0.5);
      ctx.restore();
    }
  }

  /**
   * Pseudo-3D prism: the footprint is one grid cell centred on (x, y); the top
   * face is drawn `h` pixels higher, so structures read as standing up without
   * ever claiming a cell they do not occupy.
   */
  function extrude(ctx, x, y, size, h, base, top, edge) {
    const half = size / 2;
    const x0 = x - half;
    const x1 = x + half;
    const y0 = y - half;
    const y1 = y + half;
    // Body of the extrusion (visible when looking from below the top face).
    fillPoly(ctx, [[x0, y0], [x0, y1], [x1, y1], [x1, y0 - h], [x0, y0 - h]], base);
    ctx.save();
    ctx.globalAlpha = 0.35;
    fillPoly(ctx, [[x1, y1], [x1, y0 - h], [x0, y0 - h], [x0, y0]], '#000');
    ctx.restore();
    // Top face.
    poly(ctx, [[x0, y0 - h], [x1, y0 - h], [x1, y1 - h], [x0, y1 - h]]);
    ctx.fillStyle = top;
    ctx.fill();
    ctx.strokeStyle = edge;
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  function glowSpot(ctx, x, y, radius, color, alpha) {
    const gradient = ctx.createRadialGradient(x, y, 0, x, y, radius);
    gradient.addColorStop(0, U.rgba(color, alpha == null ? 0.9 : alpha));
    gradient.addColorStop(1, U.rgba(color, 0));
    ctx.fillStyle = gradient;
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, TAU);
    ctx.fill();
  }

  function hatches(ctx, x, y, size, color, count, alpha) {
    ctx.save();
    ctx.globalAlpha = alpha == null ? 0.5 : alpha;
    ctx.strokeStyle = color;
    ctx.lineWidth = Math.max(1, size * 0.045);
    const half = size / 2;
    const step = size / (count + 1);
    for (let i = 1; i <= count; i += 1) {
      const offset = -half + step * i;
      ctx.beginPath();
      ctx.moveTo(x + offset, y + half);
      ctx.lineTo(x + half, y + offset);
      ctx.stroke();
    }
    ctx.restore();
  }

  /* -------------------------------------------------------------- stations */

  function drawStation(ctx, x, y, s, actor, theme) {
    const size = s * 1.92;
    const height = s * 0.6;
    shadow(ctx, x, y, size * 1.05, height, 0.5);
    const half = size / 2;

    // Ground apron with the team colour so the two bases never look alike.
    ctx.save();
    ctx.globalAlpha = 0.5;
    fillPoly(ctx, [
      [x - half - s * 0.22, y + half + s * 0.1],
      [x + half + s * 0.2, y + half + s * 0.1],
      [x + half + s * 0.2, y - half - s * 0.2],
      [x - half - s * 0.22, y - half - s * 0.2],
    ], U.rgba(theme.deep, 0.85));
    ctx.restore();

    // Two stacked slabs give the base its silhouette.
    extrude(ctx, x, y, size, height, '#243141', '#38495e', U.rgba(theme.main, 0.55));
    extrude(ctx, x, y - height * 0.55, size * 0.68, height * 0.72, '#2c3b4d', '#47596f', U.rgba(theme.main, 0.7));

    // Core reactor.
    const pulse = 0.72 + 0.28 * Math.sin(actor && actor.muzzle ? 0 : performance.now() / 620);
    glowSpot(ctx, x, y - height * 0.8, s * 0.66 * pulse, theme.main, 0.55);
    ctx.save();
    ctx.fillStyle = U.rgba(theme.main, 0.95);
    ctx.beginPath();
    ctx.arc(x, y - height * 0.8, s * 0.17, 0, TAU);
    ctx.fill();
    ctx.restore();

    // Corner masts.
    ctx.strokeStyle = U.rgba(theme.main, 0.85);
    ctx.lineWidth = Math.max(1.2, s * 0.05);
    for (const [dx, dy] of [[-0.36, 0.3], [0.36, 0.3], [-0.36, -0.34], [0.36, -0.34]]) {
      ctx.beginPath();
      ctx.moveTo(x + dx * size, y + dy * size);
      ctx.lineTo(x + dx * size, y + dy * size - s * 0.3);
      ctx.stroke();
    }

    // Team banner on a mast above the roof.
    ctx.save();
    ctx.strokeStyle = '#c9d6e4';
    ctx.lineWidth = Math.max(1.2, s * 0.05);
    ctx.beginPath();
    ctx.moveTo(x - half * 0.62, y - height - s * 0.3);
    ctx.lineTo(x - half * 0.62, y - height - s * 1.05);
    ctx.stroke();
    fillPoly(ctx, [
      [x - half * 0.62, y - height - s * 1.05],
      [x - half * 0.62 + s * 0.55, y - height - s * 0.9],
      [x - half * 0.62, y - height - s * 0.75],
    ], theme.main);
    ctx.restore();

    hatches(ctx, x, y - height * 0.2, size * 0.9, theme.main, 4, 0.28);
  }

  /* ---------------------------------------------------------------- people */

  function drawCrew(ctx, x, y, s, actor, theme, options) {
    const opts = options || {};
    const bodyColor = opts.body || '#e8eef7';
    const accent = opts.accent || theme.main;
    shadow(ctx, x, y, s * 0.66, 0, 0.4);
    // Legs.
    ctx.save();
    ctx.strokeStyle = '#26313e';
    ctx.lineWidth = Math.max(1.4, s * 0.06);
    ctx.beginPath();
    ctx.moveTo(x - s * 0.07, y + s * 0.02);
    ctx.lineTo(x - s * 0.1, y + s * 0.2);
    ctx.moveTo(x + s * 0.07, y + s * 0.02);
    ctx.lineTo(x + s * 0.1, y + s * 0.2);
    ctx.stroke();
    ctx.restore();
    // Torso.
    ctx.save();
    roundRect(ctx, x - s * 0.18, y - s * 0.16, s * 0.36, s * 0.34, s * 0.1);
    ctx.fillStyle = bodyColor;
    ctx.fill();
    ctx.strokeStyle = U.rgba(accent, 0.95);
    ctx.lineWidth = Math.max(1, s * 0.055);
    ctx.stroke();
    ctx.restore();
    // Shoulder pads widen the silhouette so a crew member is not a dot.
    ctx.save();
    ctx.fillStyle = '#39485a';
    for (const sx of [-1, 1]) {
      roundRect(ctx, x + sx * s * 0.18 - s * 0.07, y - s * 0.17, s * 0.14, s * 0.13, s * 0.04);
      ctx.fill();
    }
    ctx.restore();
    // Vest stripe in the team colour.
    ctx.save();
    ctx.fillStyle = U.rgba(accent, 0.9);
    ctx.fillRect(x - s * 0.18, y - s * 0.03, s * 0.36, s * 0.07);
    ctx.restore();
    // Head + helmet.
    ctx.beginPath();
    ctx.arc(x, y - s * 0.26, s * 0.115, 0, TAU);
    ctx.fillStyle = '#f3d9c0';
    ctx.fill();
    ctx.beginPath();
    ctx.arc(x, y - s * 0.295, s * 0.135, Math.PI * 1.08, Math.PI * 1.92);
    ctx.fillStyle = opts.helmet || '#2f3d4d';
    ctx.fill();
    ctx.save();
    ctx.fillStyle = U.rgba(accent, 0.95);
    ctx.fillRect(x - s * 0.13, y - s * 0.33, s * 0.26, s * 0.04);
    ctx.restore();
    if (opts.tool === 'pick') {
      // Pickaxe over the shoulder.
      ctx.save();
      ctx.strokeStyle = '#c9d6e4';
      ctx.lineWidth = Math.max(1, s * 0.05);
      ctx.beginPath();
      ctx.moveTo(x + s * 0.16, y + s * 0.16);
      ctx.lineTo(x + s * 0.34, y - s * 0.26);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(x + s * 0.22, y - s * 0.3);
      ctx.lineTo(x + s * 0.46, y - s * 0.2);
      ctx.stroke();
      ctx.restore();
    }
    if (opts.tool === 'flag') {
      ctx.save();
      ctx.strokeStyle = '#c9d6e4';
      ctx.lineWidth = Math.max(1, s * 0.045);
      ctx.beginPath();
      ctx.moveTo(x + s * 0.18, y + s * 0.2);
      ctx.lineTo(x + s * 0.18, y - s * 0.42);
      ctx.stroke();
      fillPoly(ctx, [[x + s * 0.18, y - s * 0.42], [x + s * 0.5, y - s * 0.33], [x + s * 0.18, y - s * 0.24]], accent);
      ctx.restore();
    }
    // Own crew quantities are labelled explicitly in the screen-space callout.
    if (actor && actor.owner !== 'own' && actor.backpack && actor.backpack.length) {
      ctx.save();
      ctx.fillStyle = U.rgba(PALETTE.hp, 0.95);
      ctx.font = `bold ${Math.max(8, s * 0.26)}px ui-monospace, monospace`;
      ctx.textAlign = 'left';
      ctx.fillText(String(actor.backpack.length), x + s * 0.2, y + s * 0.36);
      ctx.restore();
    }
  }

  /* ---------------------------------------------------------------- towers */

  const TOWER_STYLE = {
    gatling: { base: '#31404f', top: '#4a5b6d', barrel: '#cbd8e6', accent: '#ffd166', shape: 'quad' },
    railgun: { base: '#2b3a4d', top: '#41566f', barrel: '#9fe3ff', accent: '#7fd4ff', shape: 'long' },
    rocket: { base: '#3a2f38', top: '#54404c', barrel: '#ffb347', accent: '#ff8b5b', shape: 'rack' },
  };

  function drawTower(ctx, x, y, s, actor, theme) {
    const style = TOWER_STYLE[actor.kind] || TOWER_STYLE.gatling;
    const level = Math.max(1, Math.min(3, Number(actor.level) || 1));
    const trim = ['#94a3b8', '#67e8d1', '#ffd166'][level - 1];
    const size = s * (0.82 + level * 0.04);
    shadow(ctx, x, y, size * 1.12, s * 0.4, 0.45);
    extrude(ctx, x, y, size, s * (0.23 + level * 0.07), style.base, style.top, trim);
    if (level > 1) {
      ctx.save(); ctx.strokeStyle = trim; ctx.lineWidth = s * 0.055;
      ctx.strokeRect(x - s * 0.36, y - s * 0.45, s * 0.72, s * 0.38);
      if (level === 3) {
        ctx.fillStyle = trim;
        for (const side of [-1, 1]) ctx.fillRect(x + side * s * 0.36 - s * 0.04, y - s * 0.6, s * 0.08, s * 0.42);
      }
      ctx.restore();
    }
    // Level ring: 1/2/3 lit pips, taken from the real `level` field.
    ctx.save();
    for (let i = 0; i < 3; i += 1) {
      ctx.beginPath();
      ctx.arc(x - s * 0.24 + i * s * 0.24, y + s * 0.36, s * 0.055, 0, TAU);
      ctx.fillStyle = i < actor.level ? style.accent : 'rgba(255,255,255,0.16)';
      ctx.fill();
    }
    ctx.restore();

    const facing = actor.facing || 0;
    ctx.save();
    ctx.translate(x, y - s * 0.28);
    ctx.rotate(facing);
    const recoil = (actor.recoil || 0) * s * 0.12;
    if (style.shape === 'quad') {
      ctx.fillStyle = style.barrel;
      for (const offset of [-0.12, -0.04, 0.04, 0.12]) {
        ctx.fillRect(-recoil, offset * s - s * 0.02, s * 0.52, s * 0.045);
      }
      ctx.beginPath();
      ctx.arc(0, 0, s * 0.17, 0, TAU);
      ctx.fillStyle = style.top;
      ctx.fill();
    } else if (style.shape === 'long') {
      ctx.fillStyle = style.barrel;
      ctx.fillRect(-recoil, -s * 0.035, s * 0.66, s * 0.07);
      ctx.fillStyle = style.accent;
      ctx.fillRect(s * 0.5 - recoil, -s * 0.06, s * 0.1, s * 0.12);
      ctx.beginPath();
      ctx.arc(0, 0, s * 0.14, 0, TAU);
      ctx.fillStyle = style.top;
      ctx.fill();
    } else {
      ctx.fillStyle = style.base;
      roundRect(ctx, -s * 0.12 - recoil, -s * 0.2, s * 0.36, s * 0.4, s * 0.06);
      ctx.fill();
      ctx.fillStyle = style.barrel;
      // Launch tubes reflect the real 1/2/3 missiles per salvo.
      for (const offset of Array.from({ length: level }, (_, i) => (i - (level - 1) / 2) * 0.14)) {
        ctx.beginPath();
        ctx.arc(s * 0.2 - recoil, offset * s, s * 0.055, 0, TAU);
        ctx.fill();
      }
    }
    ctx.restore();

    if (actor.hitFlash > 0) {
      ctx.save();
      ctx.globalAlpha = actor.hitFlash * 0.7;
      glowSpot(ctx, x, y - s * 0.2, s * 0.6, PALETTE.hot, 0.8);
      ctx.restore();
    }
  }

  function drawWall(ctx, x, y, s, actor, theme) {
    const size = s * 0.96;
    const level = Math.max(1, Math.min(3, Number(actor.level) || 1));
    const height = s * (0.24 + 0.15 * (level - 1));
    shadow(ctx, x, y, size, height, 0.4);
    const base = ['#64748b', '#316c72', '#846635'][level - 1];
    const top = ['#a3b2c4', '#7dcfc3', '#ebc773'][level - 1];
    extrude(ctx, x, y, size, height, base, top, 'rgba(255,255,255,0.2)');
    // Masonry courses: two rows of offset blocks.
    ctx.save();
    ctx.strokeStyle = 'rgba(20, 30, 44, 0.5)';
    ctx.lineWidth = Math.max(1, s * 0.035);
    const half = size / 2;
    const x0 = x - half;
    const yTop = y - half - height;
    const rowH = size / 2;
    ctx.beginPath();
    ctx.moveTo(x0, yTop + rowH);
    ctx.lineTo(x0 + size, yTop + rowH);
    ctx.moveTo(x0 + size * 0.52, yTop);
    ctx.lineTo(x0 + size * 0.52, yTop + rowH);
    ctx.moveTo(x0 + size * 0.26, yTop + rowH);
    ctx.lineTo(x0 + size * 0.26, yTop + size);
    ctx.moveTo(x0 + size * 0.78, yTop + rowH);
    ctx.lineTo(x0 + size * 0.78, yTop + size);
    ctx.stroke();
    ctx.restore();
    if (actor.level > 1) {
      // Upgraded walls get a visible reinforced cap (real `level` field).
      ctx.save();
      ctx.fillStyle = level === 3 ? '#ffe3a0' : '#a2f0dd';
      ctx.fillRect(x - half, yTop - s * 0.07, size, s * 0.10);
      for (let i = 0; i < level; i += 1) {
        ctx.fillRect(x - half + size * (i + 0.5) / level - s * 0.045, yTop, s * 0.09, size * 0.75);
      }
      ctx.restore();
    }
  }

  /* ---------------------------------------------------------------- robots */

  const ROBOT_STYLE = {
    smallRobot: { body: '#8c97a6', dark: '#3c4552', size: 0.5, legs: 2, eye: '#ff6b81' },
    middleRobot: { body: '#7d8798', dark: '#333b47', size: 0.62, legs: 3, eye: '#ff8b5b' },
    largeRobot: { body: '#6f7887', dark: '#2b323c', size: 0.76, legs: 4, eye: '#ffd166' },
    bossRobot: { body: '#5f6673', dark: '#211f27', size: 0.9, legs: 4, eye: '#ff4d6d' },
  };

  function drawRobot(ctx, x, y, s, actor) {
    const style = ROBOT_STYLE[actor.kind] || ROBOT_STYLE.smallRobot;
    const size = s * style.size;
    const half = size / 2;
    shadow(ctx, x, y, size * 1.1, size * 0.4, 0.5);

    // Legs.
    ctx.save();
    ctx.strokeStyle = style.dark;
    ctx.lineWidth = Math.max(1.4, s * 0.06);
    const spread = half * 1.35;
    for (let i = 0; i < style.legs; i += 1) {
      const angle = (i / style.legs) * TAU + Math.PI / 4;
      ctx.beginPath();
      ctx.moveTo(x + Math.cos(angle) * half * 0.5, y + Math.sin(angle) * half * 0.5);
      ctx.lineTo(x + Math.cos(angle) * spread, y + Math.sin(angle) * spread * 0.8);
      ctx.stroke();
    }
    ctx.restore();

    // Chassis: small = lean wedge, middle = box, large/boss = heavy hull.
    if (actor.kind === 'smallRobot') {
      fillPoly(ctx, [[x, y + half * 0.95], [x + half, y - half * 0.5], [x, y - half * 0.9], [x - half, y - half * 0.5]], style.body);
    } else {
      ctx.save();
      roundRect(ctx, x - half, y - half * 0.85, size, size * 1.15, size * 0.22);
      ctx.fillStyle = style.body;
      ctx.fill();
      ctx.strokeStyle = style.dark;
      ctx.lineWidth = Math.max(1, s * 0.05);
      ctx.stroke();
      ctx.restore();
    }
    // Armour plating hints.
    ctx.save();
    ctx.globalAlpha = 0.5;
    ctx.fillStyle = style.dark;
    ctx.fillRect(x - half * 0.7, y - half * 0.1, size * 0.7, size * 0.12);
    ctx.restore();

    if (actor.kind === 'bossRobot') {
      ctx.save();
      ctx.fillStyle = style.dark;
      for (let i = 0; i < 6; i += 1) {
        const angle = (i / 6) * TAU;
        poly(ctx, [
          [x + Math.cos(angle) * half * 1.05, y + Math.sin(angle) * half * 1.05],
          [x + Math.cos(angle + 0.28) * half * 1.55, y + Math.sin(angle + 0.28) * half * 1.55],
          [x + Math.cos(angle - 0.28) * half * 1.55, y + Math.sin(angle - 0.28) * half * 1.55],
        ], style.dark);
        ctx.fill();
      }
      ctx.restore();
    }

    // Sensor eye, dimmed while dizzy (real abnormalState field).
    const dizzy = actor.abnormalState === 'dizzy';
    ctx.save();
    ctx.globalAlpha = dizzy ? 0.35 : 1;
    glowSpot(ctx, x, y - half * 0.35, size * 0.5, style.eye, 0.8);
    ctx.fillStyle = dizzy ? 'rgba(160,180,200,0.7)' : style.eye;
    ctx.beginPath();
    ctx.arc(x, y - half * 0.35, size * 0.13, 0, TAU);
    ctx.fill();
    ctx.restore();

    // Faction chevron: robots are a third party, never drawn as a team.
    ctx.save();
    ctx.fillStyle = U.rgba(PALETTE.robot, 0.9);
    poly(ctx, [[x, y - half - s * 0.3], [x + s * 0.09, y - half - s * 0.16], [x - s * 0.09, y - half - s * 0.16]], PALETTE.robot);
    ctx.fill();
    ctx.restore();

    if (dizzy) {
      ctx.save();
      ctx.strokeStyle = 'rgba(180, 210, 255, 0.9)';
      ctx.lineWidth = Math.max(1, s * 0.045);
      const spin = performance.now() / 260;
      for (let i = 0; i < 2; i += 1) {
        ctx.beginPath();
        ctx.ellipse(x, y - half - s * 0.34, s * 0.22, s * 0.08, spin + i * 1.1, 0, TAU);
        ctx.stroke();
      }
      ctx.restore();
    }
  }

  /* ----------------------------------------------------------- neutral map */

  function drawZone(ctx, x, y, s, zone) {
    const shape = zone.footprint || HW.footprint(zone.kind);
    const width = s * shape.width, height = s * shape.height;
    const halfX = width / 2, halfY = height / 2;
    ctx.save();
    ctx.globalAlpha = 0.9;
    roundRect(ctx, x - halfX + s * 0.06, y - halfY + s * 0.06, width - s * 0.12, height - s * 0.12, s * 0.16);
    ctx.fillStyle = U.rgba(zone.color, 0.16);
    ctx.fill();
    ctx.strokeStyle = U.rgba(zone.color, 0.75);
    ctx.lineWidth = Math.max(1, s * 0.045);
    ctx.setLineDash([s * 0.16, s * 0.12]);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.restore();

    if (zone.kind === 'stone' || zone.kind === 'iron' || zone.kind === 'copper') {
      // Crystal cluster: size reads as remaining richness only via the ring,
      // never as an invented resource amount.
      const shards = [[-0.16, 0.12, 0.34], [0.1, 0.05, 0.44], [0.0, 0.2, 0.26], [-0.02, -0.12, 0.3]];
      for (const [dx, dy, h] of shards) {
        fillPoly(ctx, [
          [x + dx * s, y + dy * s + s * 0.14],
          [x + dx * s - s * 0.09, y + dy * s + s * 0.14],
          [x + dx * s, y + dy * s + s * 0.14 - h * s],
        ], U.rgba(zone.color, 0.85));
        fillPoly(ctx, [
          [x + dx * s, y + dy * s + s * 0.14 - h * s],
          [x + dx * s + s * 0.09, y + dy * s + s * 0.14],
          [x + dx * s + s * 0.09, y + dy * s + s * 0.14],
        ], U.rgba(zone.accent, 0.5));
      }
      glowSpot(ctx, x, y, s * 0.5, zone.color, 0.28);
      ctx.save();
      ctx.fillStyle = zone.accent;
      ctx.font = `bold ${Math.max(8, s * 0.3)}px "Microsoft YaHei", sans-serif`;
      ctx.textAlign = 'center';
      ctx.fillText(zone.glyph, x, y + s * 0.42);
      ctx.restore();
      return;
    }

    if (zone.kind === 'vendor' || zone.kind === 'weaponShop') {
      const isShop = zone.kind === 'weaponShop';
      shadow(ctx, x, y, s * 0.9, s * 0.3, 0.35);
      extrude(ctx, x, y, s * 0.78, s * 0.3, isShop ? '#2f3f52' : '#3b3350',
        isShop ? '#4a6180' : '#5a4d78', U.rgba(zone.color, 0.8));
      // Awning stripes.
      ctx.save();
      ctx.globalAlpha = 0.95;
      for (let i = 0; i < 4; i += 1) {
        ctx.fillStyle = i % 2 ? U.rgba(zone.color, 0.95) : '#f2f5f9';
        ctx.fillRect(x - s * 0.34 + i * s * 0.17, y - s * 0.42, s * 0.17, s * 0.12);
      }
      ctx.restore();
      ctx.save();
      ctx.fillStyle = '#0e1622';
      ctx.font = `bold ${Math.max(8, s * 0.26)}px "Microsoft YaHei", sans-serif`;
      ctx.textAlign = 'center';
      ctx.fillText(zone.glyph, x, y + s * 0.16);
      ctx.restore();
      return;
    }

    // Task points: pulsing beacon; point 2 spans two cells (R07).
    const pulse = 0.5 + 0.5 * Math.sin(performance.now() / 700);
    glowSpot(ctx, x, y, s * (0.5 + 0.2 * pulse), zone.color, 0.3);
    ctx.save();
    ctx.strokeStyle = U.rgba(zone.color, 0.95);
    ctx.lineWidth = Math.max(1.2, s * 0.06);
    if (shape.width === 2) {
      ctx.strokeRect(x - halfX + s * 0.1, y - halfY + s * 0.1, width - s * 0.2, height - s * 0.2);
      ctx.beginPath();
      ctx.moveTo(x, y - halfY + s * 0.1);
      ctx.lineTo(x, y + halfY - s * 0.1);
      ctx.stroke();
    } else {
      ctx.beginPath();
      ctx.arc(x, y, s * 0.32, 0, TAU);
      ctx.stroke();
    }
    ctx.beginPath();
    ctx.arc(x, y, s * (0.16 + 0.12 * pulse), 0, TAU);
    ctx.fillStyle = U.rgba(zone.accent, 0.85);
    ctx.fill();
    ctx.restore();
  }

  function painterUnknown(ctx, x, y, s, actor) {
    const theme = actor.color || HW.OWNER_COLORS.neutral;
    shadow(ctx, x, y, s * 0.8, 0, 0.35);
    ctx.save();
    roundRect(ctx, x - s * 0.36, y - s * 0.36, s * 0.72, s * 0.72, s * 0.1);
    ctx.fillStyle = U.rgba(theme.main, 0.22);
    ctx.fill();
    ctx.setLineDash([s * 0.12, s * 0.08]);
    ctx.strokeStyle = theme.main;
    ctx.lineWidth = Math.max(1.2, s * 0.05);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = theme.main;
    ctx.font = `bold ${Math.max(8, s * 0.22)}px ui-monospace, monospace`;
    ctx.textAlign = 'center';
    const text = String(actor.kind || '?');
    ctx.fillText(text.length > 9 ? `${text.slice(0, 8)}…` : text, x, y + s * 0.06);
    ctx.font = `${Math.max(7, s * 0.18)}px "Microsoft YaHei", sans-serif`;
    ctx.fillText('未建模类型', x, y + s * 0.3);
    ctx.restore();
  }

  /* --------------------------------------------------------------- catalog */

  const PAINTERS = {
    station: drawStation,
    worker: (ctx, x, y, s, actor, theme) => drawCrew(ctx, x, y, s, actor, theme, { body: '#dfe8f2', tool: 'pick' }),
    pioneer: (ctx, x, y, s, actor, theme) => drawCrew(ctx, x, y, s, actor, theme, { body: '#f0e6d2', helmet: '#4a3f2e', tool: 'flag' }),
    gatling: drawTower,
    railgun: drawTower,
    rocket: drawTower,
    wall: drawWall,
    smallRobot: drawRobot,
    middleRobot: drawRobot,
    largeRobot: drawRobot,
    bossRobot: drawRobot,
  };

  function paint(ctx, actor, x, y, tile) {
    const theme = actor.color || HW.OWNER_COLORS.neutral;
    const painter = PAINTERS[actor.kind] || painterUnknown;
    painter(ctx, x, y, tile, actor, theme);
  }

  HW.sprites = {
    paint, PAINTERS, painterUnknown, drawZone,
    primitives: { poly, roundRect, fillPoly, shadow, extrude, glowSpot, hatches },
  };
}(window));
