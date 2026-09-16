/* Effects: transient visuals, each one caused by a recorded frame event.
 *
 * Lifetime rules:
 *   - Effects expire on their own clock and are hard-capped, so a long run
 *     cannot accumulate particles (verified in tests and in the browser).
 *   - Life times scale with playback speed so an 8x replay does not render a
 *     permanent fog of explosions, while a single step still shows the hit.
 *   - Nothing here decides game state: effects only read already-committed
 *     values (damage totals, death records, gold deltas).
 */
(function (global) {
  'use strict';

  const HW = global.HW = global.HW || {};
  const U = HW.util;
  const PALETTE = HW.PALETTE;

  const MAX_EFFECTS = 320;
  const SPEED_FACTOR = 0.72;

  function lifetime(base, frameMs) {
    return Math.max(150, base * SPEED_FACTOR + frameMs * 0.8);
  }

  class Effects {
    constructor() {
      this.items = [];
      this.dropped = 0;
      this.spawned = 0;
    }

    get count() { return this.items.length; }

    clear() {
      this.items.length = 0;
    }

    _push(effect) {
      this.spawned += 1;
      if (this.items.length >= MAX_EFFECTS) {
        // Oldest first: a bounded queue is what keeps long runs smooth.
        this.items.shift();
        this.dropped += 1;
      }
      this.items.push(effect);
    }

    update(now) {
      if (!this.items.length) return;
      const alive = [];
      for (const effect of this.items) {
        if (now - effect.born < effect.ttl) alive.push(effect);
      }
      this.items = alive;
    }

    /** Spawn everything a frame is responsible for, with the right sub-timing. */
    spawnForFrame(world, frame, frameMs, options) {
      const opts = options || {};
      if (!frame) return;
      const now = performance.now();
      const phase = (fraction) => now + frameMs * fraction;
      const tile = opts.tile || 24;
      const center = (pos, size) => ({
        x: pos.x * tile + tile / 2 + (size === 2 ? tile / 2 : 0),
        y: (opts.height - 1 - pos.y) * tile + tile / 2 - (size === 2 ? tile / 2 : 0),
      });

      // 1. Shots resolve before movement (official order, R02/R04).
      for (const action of frame.actions || []) {
        if (action.a !== 'attack' || !action.salvos) continue;
        const from = center(action.from, 1);
        const kind = action.kind;
        action.salvos.forEach((salvo, index) => {
          const to = center(salvo.cell, 1);
          const ballistic = kind !== 'rocket' && !!salvo.path;
          const born = phase(0.06 + index * 0.05);
          const flight = Math.max(90, frameMs * 0.28);
          const impactAt = kind === 'rocket' ? born + flight : phase(0.34 + index * 0.05);
          this._push({
            type: kind === 'railgun' ? 'beam' : (kind === 'rocket' ? 'rocket' : 'tracer'),
            from, to, born,
            ttl: kind === 'rocket' ? flight : lifetime(kind === 'railgun' ? 320 : 420, frameMs),
            color: kind === 'rocket' ? PALETTE.robot : (kind === 'railgun' ? '#9fe3ff' : '#ffe08a'),
            kind,
            // The recorded path is the real line the projectile travelled; the
            // renderer draws it instead of assuming a straight tower->cell line.
            path: ballistic ? salvo.path.map((cell) => center(cell, 1)) : null,
            blocked: !!salvo.blocked,
            energy: salvo.energy,
          });
          if (opts.spawnHits !== false) {
            // An impact only exists where something was actually hit: a bullet
            // that flew through empty ground must not puff on the ground.
            const anyHit = (salvo.hits || []).length > 0;
            if (anyHit || !ballistic) {
              this._push({
                type: kind === 'rocket' ? 'explosion' : 'impact', at: to, born: impactAt,
                ttl: lifetime(340, frameMs),
                color: kind === 'rocket' ? '#ffb347' : '#ffe08a',
                radius: tile * (kind === 'rocket' ? 1.5 : 1.1),
              });
            }
          }
          // One damage number per robot the salvo actually hit, in path order.
          const hits = salvo.hits || [];
          hits.forEach((hit, hitIndex) => {
            const at = hit.cell ? center(hit.cell, 1) : to;
            this._push({
              type: 'damage', at, value: hit.damage, born: impactAt + frameMs * (0.02 + hitIndex * 0.04),
              ttl: lifetime(900, frameMs), color: '#ffd9a0', robot: hit.robot,
            });
          });
        });
      }

      // 2. Robot actions: movement then attacks.
      for (const move of frame.robotMoves || []) {
        const at = center(move.to, 1);
        this._push({
          type: 'dust', at, born: phase(0.45), ttl: lifetime(460, frameMs),
          color: 'rgba(200, 210, 225, 0.5)', radius: tile * 0.7,
        });
      }
      for (const attack of frame.robotAttacks || []) {
        const to = center(attack.to, 1);
        this._push({
          type: 'beam', from: center(attack.from, 1), to, born: phase(0.5),
          ttl: lifetime(300, frameMs), color: PALETTE.robot, kind: 'robotClaw',
        });
        this._push({
          type: 'impact', at: to, born: phase(0.52), ttl: lifetime(300, frameMs),
          color: PALETTE.robot, radius: tile * 1.1,
        });
        this._push({
          type: 'damage', at: to, value: attack.damage, born: phase(0.54),
          ttl: lifetime(900, frameMs), color: '#ff9aa8',
          // The record names either a unit (`victim`) or a building; keeping both
          // means a wall hit is never rendered as a role taking damage.
          victim: attack.victim, building: attack.building,
          label: attack.building !== undefined && attack.building !== null
            ? '拆建筑' : '',
        });
      }

      // 3. Deaths and rewards.
      for (const death of frame.robotDeaths || []) {
        const at = center(death.pos, 1);
        this._push({
          type: 'explosion', at, born: phase(0.66), ttl: lifetime(780, frameMs),
          radius: tile * 1.9, color: '#ffcf7a',
        });
        this._push({
          type: 'scorch', at, born: phase(0.66), ttl: lifetime(12000, frameMs), radius: tile * 0.55,
        });
        this._push({
          type: 'float', at, text: `击杀 +${death.score}`, born: phase(0.72),
          ttl: lifetime(1300, frameMs), color: PALETTE.hp,
        });
      }
      for (const death of frame.unitDeaths || []) {
        const at = center(death.pos, 1);
        this._push({
          type: 'explosion', at, born: phase(0.66), ttl: lifetime(820, frameMs),
          radius: tile * 1.5, color: death.kind === 'station' ? '#ff6b81' : '#ff9aa8',
        });
        this._push({
          type: 'float', at, text: `${U.kindName(death.kind)} 阵亡`, born: phase(0.7),
          ttl: lifetime(1500, frameMs), color: '#ff9aa8',
        });
        if (death.kind === 'station' || death.kind === 'wall' || HW.OFFICIAL.towerTypes.includes(death.kind)) {
          this._push({ type: 'rubble', at, born: phase(0.68), ttl: lifetime(20000, frameMs), radius: tile * 0.5 });
        }
      }

      // Compare adjacent recorded states, never the last frame the viewer saw.
      // Seeking backwards/forwards therefore cannot invent a level-up.
      const previous = world.states && world.index > 0 ? world.states[world.index - 1] : null;
      if (previous) {
        for (const [group, owner] of [['teamOur', 'own'], ['teamEnemy', 'enemy']]) {
          const prior = new Map(((previous[group] || {}).roles || []).map(r => [String(r.id), r]));
          for (const actor of world.actors || []) {
            const before = prior.get(String(actor.id));
            if (actor.owner !== owner || !before || before.roleType !== actor.kind
                || !(Number(actor.level) > Number(before.level || 1))) continue;
            if (actor.kind !== 'wall' && !HW.OFFICIAL.towerTypes.includes(actor.kind) && actor.kind !== 'station') continue;
            this._push({ type: 'float', at: center(actor.pos, actor.size),
              text: `${U.kindName(actor.kind)} Lv.${before.level || 1} → ${actor.level}`,
              born: phase(0.4), ttl: lifetime(1200, frameMs), color: '#ffe08a' });
          }
        }
      }

      // 4. Arrivals at dusk, and economy changes.
      const arrivals = frame.spawned || [];
      for (const spawn of arrivals) {
        const at = center(spawn.pos, 1);
        this._push({
          type: 'warp', at, born: phase(0.6), ttl: lifetime(700, frameMs),
          radius: tile * 1.3, color: PALETTE.robot,
        });
      }
      if (arrivals.length) {
        const points = arrivals.map(spawn => center(spawn.pos, 1));
        const at = { x: points.reduce((sum, p) => sum + p.x, 0) / points.length,
          y: Math.max(tile * 0.5, Math.min(...points.map(p => p.y)) - tile * 0.8) };
        this._push({ type: 'float', at,
          text: arrivals.length === 1 ? `${U.kindName(arrivals[0].kind)} 出现` : `机器人出现 ×${arrivals.length}`,
          born: phase(0.62), ttl: lifetime(1100, frameMs), color: PALETTE.robot });
      }
      if (frame.gold && frame.gold.before !== frame.gold.after) {
        const actor = (world.actors || []).find((a) => a.kind === 'station' && a.owner === 'own');
        if (actor) {
          const at = center(actor.pos, 2);
          const delta = frame.gold.after - frame.gold.before;
          this._push({
            type: 'float', at: { x: at.x, y: at.y - tile * 1.2 },
            text: `${delta > 0 ? '+' : ''}${delta} 金币`, born: phase(0.4),
            ttl: lifetime(1200, frameMs), color: delta > 0 ? '#ffe08a' : '#ff9aa8',
          });
        }
      }
      if (frame.skipped && frame.skipped.length && opts.onSkip) {
        opts.onSkip(frame.skipped, frame);
      }
    }

    /** Spark for a failed command, anchored on the unit that failed. */
    failed(world, id, tile, height) {
      const actor = (world.actors || []).find((a) => String(a.id) === String(id));
      if (!actor) return;
      const at = {
        x: actor.pos.x * tile + tile / 2,
        y: (height - 1 - actor.pos.y) * tile + tile / 2,
      };
      this._push({
        type: 'float', at, text: '指令未执行', born: performance.now(),
        ttl: 1400, color: '#ffb3c0',
      });
      this._push({ type: 'impact', at, born: performance.now(), ttl: 420, color: '#ff8ba0', radius: tile });
    }

    draw(ctx, now) {
      for (const effect of this.items) {
        const age = now - effect.born;
        if (age < 0) continue;
        const t = U.clamp(age / effect.ttl, 0, 1);
        switch (effect.type) {
          case 'tracer': this._tracer(ctx, effect, t); break;
          case 'beam': this._beam(ctx, effect, t); break;
          case 'rocket': this._rocket(ctx, effect, t); break;
          case 'impact': this._impact(ctx, effect, t); break;
          case 'explosion': this._explosion(ctx, effect, t); break;
          case 'damage': this._floatText(ctx, effect, t,
            `${effect.label ? effect.label + ' ' : ''}-${effect.value}`); break;
          case 'float': this._floatText(ctx, effect, t, effect.text); break;
          case 'dust': this._soft(ctx, effect, t); break;
          case 'warp': this._warp(ctx, effect, t); break;
          case 'scorch': this._scorch(ctx, effect, t); break;
          case 'rubble': this._rubble(ctx, effect, t); break;
          default: break;
        }
      }
    }

    _tracer(ctx, effect, t) {
      // The recorded path is the real line the bullet travelled, already mapped
      // to screen space at spawn time; fall back to the two endpoints when the
      // frame came from the older point-damage records.
      const path = (effect.path && effect.path.length >= 2)
        ? effect.path
        : [effect.from, effect.to];
      ctx.save();
      ctx.globalAlpha = (1 - t) * 0.95;
      ctx.strokeStyle = effect.color;
      ctx.lineWidth = 2.2 * (1 - t * 0.6);
      ctx.setLineDash([9, 7]);
      ctx.lineDashOffset = -t * 40;
      ctx.beginPath();
      ctx.moveTo(path[0].x, path[0].y);
      for (let i = 1; i < path.length; i += 1) ctx.lineTo(path[i].x, path[i].y);
      ctx.stroke();
      if (effect.blocked) {
        // Where the bullet was consumed: makes "hits the nearest robot on the
        // path" visible instead of implied.
        const end = path[path.length - 1];
        ctx.setLineDash([]);
        ctx.globalAlpha = (1 - t) * 0.8;
        ctx.beginPath();
        ctx.arc(end.x, end.y, 4 + 4 * t, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.restore();
    }

    _beam(ctx, effect, t) {
      const path = effect.path && effect.path.length >= 2 ? effect.path : [effect.from, effect.to];
      ctx.save(); ctx.globalAlpha = 1 - t;
      ctx.strokeStyle = effect.color; ctx.lineWidth = 3.4 * (1 - t * 0.7);
      ctx.shadowColor = effect.color; ctx.shadowBlur = 12;
      ctx.beginPath(); ctx.moveTo(path[0].x, path[0].y);
      for (const point of path.slice(1)) ctx.lineTo(point.x, point.y);
      ctx.stroke();
      ctx.strokeStyle = '#effbff'; ctx.lineWidth = 1;
      ctx.stroke(); ctx.restore();
    }

    _rocket(ctx, effect, t) {
      const { from, to } = effect;
      const lift = Math.min(32, Math.hypot(to.x - from.x, to.y - from.y) * 0.12);
      const point = (v) => ({ x: U.lerp(from.x, to.x, v), y: U.lerp(from.y, to.y, v) - Math.sin(Math.PI * v) * lift });
      const head = point(t), tail = point(Math.max(0, t - 0.10));
      // Short smoke trail and a solid missile, never a full-length energy beam.
      ctx.save(); ctx.strokeStyle = 'rgba(240,190,140,0.55)'; ctx.lineWidth = 3;
      ctx.beginPath(); ctx.moveTo(tail.x, tail.y); ctx.lineTo(head.x, head.y); ctx.stroke();
      ctx.translate(head.x, head.y);
      ctx.rotate(Math.atan2(to.y - from.y - Math.PI * lift * Math.cos(Math.PI * t), to.x - from.x));
      ctx.fillStyle = '#ff7c35';
      ctx.beginPath(); ctx.moveTo(-5, -2); ctx.lineTo(-11, 0); ctx.lineTo(-5, 2); ctx.fill();
      ctx.fillStyle = '#fff0c0'; ctx.fillRect(-5, -2.5, 9, 5);
      ctx.fillStyle = '#ffb347'; ctx.beginPath(); ctx.moveTo(4, -2.5); ctx.lineTo(8, 0); ctx.lineTo(4, 2.5); ctx.fill();
      ctx.restore();
    }

    _impact(ctx, effect, t) {
      const radius = (effect.radius || 24) * (0.35 + U.easeOut(t) * 0.9);
      ctx.save();
      ctx.globalAlpha = (1 - t) * 0.9;
      ctx.strokeStyle = effect.color;
      ctx.lineWidth = 2.6 * (1 - t);
      ctx.beginPath();
      ctx.arc(effect.at.x, effect.at.y, radius, 0, Math.PI * 2);
      ctx.stroke();
      ctx.globalAlpha = (1 - t) * 0.35;
      ctx.fillStyle = effect.color;
      ctx.beginPath();
      ctx.arc(effect.at.x, effect.at.y, radius * 0.4, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }

    _explosion(ctx, effect, t) {
      const radius = (effect.radius || 34) * U.easeOut(t);
      ctx.save();
      const gradient = ctx.createRadialGradient(effect.at.x, effect.at.y, 0, effect.at.x, effect.at.y, Math.max(1, radius));
      gradient.addColorStop(0, `rgba(255, 250, 220, ${(1 - t) * 0.95})`);
      gradient.addColorStop(0.45, U.rgba('#ffb347', (1 - t) * 0.7));
      gradient.addColorStop(1, 'rgba(255, 120, 60, 0)');
      ctx.fillStyle = gradient;
      ctx.beginPath();
      ctx.arc(effect.at.x, effect.at.y, radius, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = (1 - t) * 0.85;
      ctx.strokeStyle = '#fff3c4';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(effect.at.x, effect.at.y, radius * 0.85, 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();
    }

    _floatText(ctx, effect, t, text) {
      const rise = U.easeOut(t) * 26;
      ctx.save();
      ctx.globalAlpha = t < 0.15 ? t / 0.15 : (1 - (t - 0.15) / 0.85);
      ctx.font = 'bold 13px "Microsoft YaHei", sans-serif';
      ctx.textAlign = 'center';
      ctx.lineWidth = 3;
      ctx.strokeStyle = 'rgba(6, 12, 20, 0.85)';
      ctx.strokeText(text, effect.at.x, effect.at.y - 6 - rise);
      ctx.fillStyle = effect.color;
      ctx.fillText(text, effect.at.x, effect.at.y - 6 - rise);
      ctx.restore();
    }

    _soft(ctx, effect, t) {
      ctx.save();
      ctx.globalAlpha = (1 - t) * 0.5;
      ctx.fillStyle = effect.color;
      ctx.beginPath();
      ctx.arc(effect.at.x, effect.at.y, (effect.radius || 14) * (0.6 + t), 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }

    _warp(ctx, effect, t) {
      ctx.save();
      ctx.globalAlpha = (1 - t) * 0.8;
      ctx.strokeStyle = effect.color;
      ctx.lineWidth = 2;
      for (let i = 0; i < 3; i += 1) {
        ctx.beginPath();
        ctx.arc(effect.at.x, effect.at.y, (effect.radius || 26) * (1 - t) * (1 - i * 0.22), 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.restore();
    }

    _scorch(ctx, effect, t) {
      ctx.save();
      ctx.globalAlpha = Math.min(0.6, (1 - t) * 0.6);
      ctx.fillStyle = 'rgba(10, 12, 16, 0.9)';
      ctx.beginPath();
      ctx.ellipse(effect.at.x, effect.at.y, effect.radius || 14, (effect.radius || 14) * 0.7, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }

    _rubble(ctx, effect, t) {
      ctx.save();
      ctx.globalAlpha = Math.min(0.55, (1 - t) * 0.55);
      ctx.fillStyle = 'rgba(70, 80, 95, 0.9)';
      for (let i = 0; i < 5; i += 1) {
        const angle = (i / 5) * Math.PI * 2;
        const radius = (effect.radius || 12) * (0.5 + (i % 2) * 0.5);
        ctx.fillRect(effect.at.x + Math.cos(angle) * radius, effect.at.y + Math.sin(angle) * radius * 0.7, 4, 3);
      }
      ctx.restore();
    }
  }

  HW.Effects = Effects;
  HW.MAX_EFFECTS = MAX_EFFECTS;
}(window));
