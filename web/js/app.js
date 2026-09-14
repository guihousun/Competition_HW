/* App controller: wiring, playback clock, input handling, error surfaces.
 *
 * Lifecycle of the whole product in one place:
 *   scenario -> official-shaped observation -> POST strategy -> commands ->
 *   local settle (/debug/step or /debug/series) -> frame -> canvas + panels.
 *
 * Playback is driven by recorded frames only. In live mode a "next frame" is a
 * real /debug/step call; in record mode it just moves a cursor over frames that
 * were already produced by the same step(). Neither mode invents state.
 */
(function (global) {
  'use strict';

  const HW = global.HW;
  const U = HW.util;

  const PRESETS = [
    { seed: 1, side: 'challenger', pressure: 1 },
    { seed: 7, side: 'defender', pressure: 3 },
    { seed: 19, side: 'challenger', pressure: 2 },
    { seed: 23, side: 'defender', pressure: 1 },
  ];

  class App {
    constructor() {
      this.world = null;
      this.renderer = new HW.Renderer(document.getElementById('map'));
      this.effects = new HW.Effects();
      this.panel = new HW.Panel();
      this.sampleState = null;
      this.playing = false;
      this.frameMs = 550;
      this.lastFrameIndex = -1;
      this.frameStartedAt = 0;
      this.effectsSpawnedFor = -1;
      this.previewCommands = {};
      this.executedCommands = {};
      // Blocking work (scene, recording, import) locks the transport; a routine
      // /debug/step is tracked separately so playback never greys anything out.
      this.busy = false;
      this.stepping = false;
      this.stepToken = null;
      // Bumped whenever the scene on screen is replaced or the user jumps: any
      // response still in flight belongs to the old view and must be dropped.
      this.generation = 0;
      // Input in progress that frame updates must not overwrite.
      this.seekDraft = false;
      this.scrubActive = false;
      this.follow = false;
      this.hover = null;
      this.selected = null;
      this.walkProgress = 1;
      this.fps = 0;
      this.sampleTimes = [];
      this.timings = { scenario: 0, step: 0, series: 0 };
      this.stats = null;
      this.presetIndex = 0;
      this.lastNow = performance.now();
      this.dragging = null;
      this.fitMode = true;
      // Local task rewards seen this match, for the task panel.
      this.taskRewards = [];
      // Local two-team job state and its poller.
      this.twoMatch = null;
      this.twoMatchTimer = null;
      this.recordingId = null;
      this.recordingPoll = false;
      this.markers = [];
    }

    /* ------------------------------------------------------------- boot */
    async boot() {
      this.bind();
      if (HW.guide) HW.guide.install(this);
      this.panel.collapse(true);
      this.renderer.options.preview = false;
      this.renderer.options.lighting = false;
      this.resize();
      await this.loadStats();
      await this.loadRules();
      this.panel.setMode('idle');
      this.panel.updateHud(this.placeholderWorld(), { label: '待机', detail: '尚未创建对局' });
      this.panel.renderDiagnostics(this.placeholderWorld(), this.diagInfo());
      this.panel.empty(true, '点击开始，策略会自动控制工人、开拓者和炮台。你可以随时暂停，查看它为什么这样行动。');
      this.loop();
      try {
        const saved = sessionStorage.getItem('hw-recording');
        if (saved) { this.recordingId = saved; this.watchRecording(); }
      } catch (error) { void error; }
    }

    placeholderWorld() {
      return {
        actors: [], zones: [], frameCount: 0, index: 0, maxIndex: 0, roundNo: 1,
        phase: HW.phaseInfo(1), gold: 75, score: 0, kills: 0, mode: 'idle',
        diagnostics: { unknownKinds: new Set(), uncoveredActions: new Set(), notes: [] },
        count: () => ({ own: 0, robots: 0, enemy: 0, workers: 0, pioneers: 0, towers: 0, walls: 0 }),
        counts: () => ({ own: 0, robots: 0, enemy: 0, workers: 0, pioneers: 0, towers: 0, walls: 0 }),
        stationHealth: () => ({ alive: 0, total: 0, max: 1, dead: false }),
        describe: () => [],
        states: [{}],
        result: () => ({ round: 1, score: 0, kills: 0 }),
      };
    }

    async loadStats() {
      try {
        const stats = await this.getJson('/debug/stats');
        HW.runtime.stats = stats;
        Object.assign(HW.OFFICIAL, {
          width: stats.mapWidth, height: stats.mapHeight,
          dayRounds: stats.dayRounds, nightRounds: stats.nightRounds,
          roundsPerDay: stats.roundsPerDay, maxRounds: stats.maxRounds,
          weaponCost: stats.weaponCost, towerLimit: stats.towerLimit,
          towerTypes: stats.towerTypes, towerRangeByLevel: stats.towerRangeByLevel,
          healthByLevel: stats.healthByLevel, unitHealth: stats.unitHealth,
          robotAttack: stats.robotAttack, robotScore: stats.robotScore,
        });
        HW.runtime.statsLoaded = true;
      } catch (error) {
        this.panel.toast('未取到 /debug/stats，使用内置常量（可能过期）。', 'warn');
      }
    }

    async loadRules() {
      try {
        const payload = await this.getJson('/debug/rules');
        HW.runtime.rules = payload;
        this.panel.renderRules(payload);
      } catch (error) {
        this.panel.renderRules({ rows: [] });
        this.panel.toast('规则边界表加载失败，请查看 /debug/rules。', 'warn');
      }
    }

    /* --------------------------------------------------------- requests */
    async getJson(url) {
      const response = await fetch(url, { headers: { Accept: 'application/json' }, signal: AbortSignal.timeout(15000) });
      const data = await response.json().catch(() => ({ error: '响应不是 JSON' }));
      if (!response.ok) throw new Error(data.error || `请求失败 ${response.status}`);
      return data;
    }

    async postJson(url, payload) {
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(15000),
      });
      const data = await response.json().catch(() => ({ error: '响应不是 JSON' }));
      if (!response.ok) throw new Error(data.error || `请求失败 ${response.status}`);
      return data;
    }

    /* ------------------------------------------------------ main actions */
    async startNewMatch(options) {
      if (this.busy) return;
      if (await this.newMatch(options)) this.play();
    }

    async startLLMDemo() {
      if (this.busy) return;
      this.stop(); this.busy = true; this.panel.setBusy(true);
      let ready = false;
      try {
        const payload = await this.postJson('/debug/llm/scenario', {seed: Number(document.getElementById('seed').value), side: document.getElementById('side').value});
        this.world = HW.World.fromScenario(payload);
        this.panel.empty(false); this.afterWorldChange(); this.renderer.fit(this.world);
        this.panel.toast('已启用真实 DeepSeek API：演示任务会调用模型，等待时暂停推进。');
        ready = true;
      } catch (error) { this.panel.toast('LLM 演示启动失败：' + error.message, 'error'); }
      finally { this.busy = false; this.panel.setBusy(false); }
      if (ready) this.play();
    }

    openDebug(name) {
      this.panel.collapse(false);
      this.panel.activateTab(name || 'plan');
      document.getElementById('dev').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    async newMatch(options) {
      if (this.busy) return;
      this.busy = true;
      const opts = options || {};
      const seed = Number(opts.seed != null ? opts.seed : document.getElementById('seed').value);
      const side = opts.side || document.getElementById('side').value;
      const pressure = Number(opts.pressure != null ? opts.pressure : document.getElementById('pressure').value);
      this.stop();
      this.panel.setBusy(true);
      this.panel.status('创建场景…');
      try {
        const started = performance.now();
        const payload = await this.getJson(`/debug/scenario?seed=${encodeURIComponent(seed)}&side=${encodeURIComponent(side)}&pressure=${encodeURIComponent(pressure)}`);
        this.timings.scenario = performance.now() - started;
        this.world = HW.World.fromScenario(payload);
        this.panel.empty(false);
        this.afterWorldChange();
        document.getElementById('seed').value = String(seed);
        document.getElementById('side').value = side;
        document.getElementById('pressure').value = String(pressure);
        this.panel.setMode('live', `seed ${seed}`);
        this.panel.status('已就绪');
        this.panel.toast(`新对局：种子 ${seed} · ${side === 'challenger' ? '蓝方' : '红方'} · 压力 ${pressure}`, null);
        this.renderer.fit(this.world);
        await this.refreshPreview();
        return true;
      } catch (error) {
        this.panel.setMode('idle');
        this.panel.status('创建失败');
        this.panel.toast(`场景创建失败：${error.message}`, 'error');
        this.panel.empty(true, '场景创建失败，请检查服务是否仍在运行（python main.py 8080）。');
        return false;
      } finally {
        this.busy = false;
        this.panel.setBusy(false);
        if (this.world) this.panel.updateHud(this.world, this.statusInfo());
      }
    }

    async loadSeries(limit) {
      if (this.busy) return;
      const seed = Number(document.getElementById('seed').value);
      const side = document.getElementById('side').value;
      const pressure = Number(document.getElementById('pressure').value);
      const full = !limit;
      this.stop();
      this.busy = true;
      this.panel.setBusy(true);
      this.panel.status('录制完整对局…');
      this.panel.toast(full
        ? '正在后台录制到终局（最多 1300 回合），可以查看进度或取消…'
        : `正在录制 ${limit} 回合本地对局，请稍候…`, null);
      try {
        const started = performance.now();
        const job = await this.postJson('/debug/recording/start', { seed, side, pressure, limit: limit || 1300 });
        this.recordingId = job.id;
        try { sessionStorage.setItem('hw-recording', job.id); } catch (error) { void error; }
        this.timings.series = performance.now() - started;
        await this.watchRecording();
      } catch (error) {
        this.panel.status('录制失败');
        this.panel.toast(`录制失败：${error.message}`, 'error');
      } finally {
        if (!this.recordingId) {
          this.busy = false; this.panel.setBusy(false);
          if (this.world) this.panel.updateHud(this.world, this.statusInfo());
        }
      }
    }

    showRecordingProgress(job) {
      document.getElementById('recording-progress').hidden = false;
      document.getElementById('recording-title').textContent = job.state === 'stopping' ? '正在停止…' : '后台录制中';
      document.getElementById('recording-detail').textContent = `${job.round} / ${job.limit} 回合 · ${Math.round(job.progress * 100)}% · ${job.elapsed}s`;
      document.getElementById('recording-meter').value = job.progress;
      document.getElementById('recording-cancel').disabled = job.state !== 'running';
      document.getElementById('recording-retry').hidden = true;
    }

    async watchRecording() {
      if (!this.recordingId || this.recordingPoll) return;
      this.recordingPoll = true;
      this.busy = true;
      this.stop();
      this.panel.setBusy(true);
      try {
        while (this.recordingId) {
          const job = await this.getJson(`/debug/recording?id=${encodeURIComponent(this.recordingId)}`);
          this.showRecordingProgress(job);
          if (job.state === 'failed') throw new Error(job.error || '录制失败');
          if (job.state === 'done' || job.state === 'cancelled') {
            const recording = await this.getJson(`/debug/recording/result?id=${encodeURIComponent(job.id)}`);
            this.openRecording(recording);
            this.timings.series = job.elapsed * 1000;
            this.panel.toast(`${job.state === 'cancelled' ? '录制已取消，保留' : '录制完成，共'} ${recording.frames.length} 回合。从初始帧播放或跳转关键事件。`);
            this.clearRecording();
            break;
          }
          await new Promise((resolve) => setTimeout(resolve, 500));
        }
      } catch (error) {
        this.panel.toast(`录制连接中断：${error.message}`, 'error');
        document.getElementById('recording-progress').hidden = false;
        document.getElementById('recording-title').textContent = '无法获取录制';
        document.getElementById('recording-detail').textContent = error.message;
        document.getElementById('recording-retry').hidden = false;
        document.getElementById('recording-cancel').disabled = false;
      } finally {
        this.recordingPoll = false;
        if (!this.recordingId) {
          this.busy = false; this.panel.setBusy(false);
          if (this.world) this.panel.updateHud(this.world, this.statusInfo());
        }
      }
    }

    clearRecording() {
      this.recordingId = null;
      try { sessionStorage.removeItem('hw-recording'); } catch (error) { void error; }
      document.getElementById('recording-progress').hidden = true;
    }

    async cancelRecording() {
      if (!this.recordingId) return;
      try {
        const job = await this.postJson('/debug/recording/cancel', { id: this.recordingId });
        if (job.state === 'failed') {
          this.clearRecording(); this.busy = false; this.panel.setBusy(false); return;
        }
        if (!this.recordingPoll) this.watchRecording();
      } catch (error) {
        this.panel.toast(`无法取消服务器任务：${error.message}。已断开页面追踪。`, 'warn');
        this.clearRecording();
        this.busy = false;
        this.panel.setBusy(false);
      }
    }

    openRecording(recording) {
      HW.Replay.validate(recording);
      const world = HW.World.fromRecording(recording);
      this.stop();
      this.world = world;
      this.panel.empty(false);
      this.afterWorldChange();
      this.applyCurrentFrame(false, false);
      this.panel.setMode('record', `${world.metadata.recordingStatus === 'cancelled' ? '已取消 · ' : ''}${world.frameCount} 回合 · seed ${world.seed}`);
      this.renderer.fit(world);
    }

    afterWorldChange() {
      this.invalidateStep();
      this.waitingLLM = false;
      this.follow = false;
      // A new scene replaces every input draft: the frame number field must show
      // the new recording, not a number typed against the old one.
      this.seekDraft = false;
      document.getElementById('follow').classList.remove('primary');
      this.effects.clear();
      this.lastFrameIndex = this.world.index;
      this.effectsSpawnedFor = -1;
      this.frameStartedAt = performance.now();
      this.walkProgress = 1;
      this.selected = null;
      this.renderer.selected = null;
      this.hover = null;
      this.previewCommands = {};
      this.executedCommands = {};
      this.bannerHtml = '';
      this.panel.banner('');
      this.panel.setResponse('尚未请求。', '根路径 POST 的兼容响应。');
      // A new scene is a new request: stale edits from the old one are discarded.
      this.panel.setRequest(JSON.stringify(this.world.states[0], null, 2), true);
      this.panel.acceptRequestEdit();
      this.panel.activateTab('plan');
      this.panel.inspect(null);
      document.getElementById('scrub').max = String(this.world.maxIndex);
      this.refreshReplayTools();
    }

    /**
     * The scene on screen changed: every in-flight response is now obsolete, and
     * a step that is still running must not block the new scene.
     */
    invalidateStep() {
      this.generation += 1;
      this.stepping = false;
      this.stepToken = null;
      this.panel.setStepPending(false);
    }

    /** True when an awaited response no longer belongs to the visible scene. */
    staleResponse(world, token, index) {
      return this.world !== world || this.generation !== token || world.index !== index;
    }

    /* ------------------------------------------------------------ clock */
    statusInfo() {
      if (this.waitingLLM) return {label: '等待 DeepSeek', detail: '模型返回前不推进游戏回合'};
      if (!this.world) return { label: '待机', detail: '尚未创建对局' };
      if (this.playing) return { label: '播放中', detail: `每 ${this.frameMs} ms 一帧` };
      if (this.stepping) return { label: '正在结算', detail: '等待本地 /debug/step' };
      if (this.busy) return { label: '请求中', detail: '等待本地结算' };
      if (this.world.done && this.world.index === this.world.maxIndex) return { label: '已结束', detail: this.world.result().reason || '终局' };
      if (this.world.index === 0) return { label: '已就绪', detail: '显示初始布局' };
      return { label: '已暂停', detail: `第 ${this.world.roundNo} 回合` };
    }

    /** Local two-team job: poll while it runs, then stop and show the result. */
    startTwoMatch() {
      const seedInput = document.getElementById('twomatch-seed');
      const roundsInput = document.getElementById('twomatch-rounds');
      const seed = Number((seedInput && seedInput.value) || 1);
      const rounds = Number((roundsInput && roundsInput.value) || 300);
      this.twoMatch = { state: 'running', round: 0, progress: 0, seed,
                        maxRounds: rounds, sides: ['challenger', 'defender'],
                        scores: {}, baseHp: {} };
      this.panel.renderTwoMatch(this.twoMatch);
      fetch(`/debug/twomatch/start?seed=${encodeURIComponent(seed)}&rounds=${encodeURIComponent(rounds)}`)
        .then((response) => response.json())
        .then((data) => { this.twoMatch = data; this.panel.renderTwoMatch(data); })
        .catch((error) => { this.twoMatch = { state: 'failed', error: String(error) };
                            this.panel.renderTwoMatch(this.twoMatch); });
      if (this.twoMatchTimer) clearInterval(this.twoMatchTimer);
      this.twoMatchTimer = setInterval(() => {
        fetch('/debug/twomatch').then((response) => response.json()).then((data) => {
          this.twoMatch = data;
          this.panel.renderTwoMatch(data);
          if (data.state !== 'running' && this.twoMatchTimer) {
            clearInterval(this.twoMatchTimer);
            this.twoMatchTimer = null;
          }
        }).catch(() => {
          if (this.twoMatchTimer) clearInterval(this.twoMatchTimer);
          this.twoMatchTimer = null;
        });
      }, 1000);
    }

    stopTwoMatch() {
      fetch('/debug/twomatch/stop').then((response) => response.json()).then((data) => {
        this.twoMatch = data;
        this.panel.renderTwoMatch(data);
      }).catch(() => {});
    }

    /** Task rewards recorded so far, read straight out of the recorded frames. */
    collectRewards() {
      const world = this.world;
      if (!world) return [];
      const rewards = [];
      for (let index = 1; index <= world.index; index += 1) {
        const state = world.states[index];
        const report = state && state._demo && state._demo.task_report;
        if (report && report.rewards) {
          rewards.push({ round: (state.roundNo || index) - 1, at: index,
                         rate: report.rewards.rate, score: report.rewards.score });
        }
      }
      return rewards;
    }

    diagInfo() {
      const state = (this.world && this.world.state) || {};
      const meta = state._demo || {};
      const planner = meta.planner || {};
      const judge = planner.judge || {};
      return {
        effects: this.effects.count,
        dropped: this.effects.dropped,
        fps: this.fps,
        frameMs: this.frameTime || 0,
        timings: this.timings,
        taskReport: meta.task_report || null,
        taskEvents: meta.task_events || [],
        rewards: this.collectRewards(),
        judge: {
          llmUsed: judge.llmUsedToday ?? judge.llm_used_today,
          llmResponses: judge.llmResponses ?? judge.llm_responses ?? 0,
          pendingPrompt: (judge.pendingPrompt || judge.pending_prompt || {}).payload,
          pendingCommand: (judge.pendingCmd || judge.pending_cmd || {}).payload,
          lastResult: (judge.lastResult || judge.last_result || {}).raw,
          llmResp: state.llmResp,
        },
      };
    }

    loop() {
      const step = (now) => {
        const dt = now - this.lastNow;
        this.lastNow = now;
        if (dt > 0) this.fps = this.fps * 0.9 + (1000 / dt) * 0.1;
        if (this.world) {
          const since = now - this.frameStartedAt;
          this.walkProgress = this.frameMs <= 0 ? 1 : U.clamp(since / Math.max(80, this.frameMs * 0.8), 0, 1);
          const effectsStart = performance.now();
          this.effects.update(now);
          const renderStart = performance.now();
          this.renderer.draw(this.world, this.effects, {
            walkProgress: this.walkProgress,
            previewCommands: this.previewCommands,
            executedCommands: this.executedCommands,
            hoverActor: this.hover,
            showAllRanges: this.showAllRanges,
            trails: this.walkProgress < 0.85 ? this.trailFromFrame() : [],
          }, now);
          this.frameTime = performance.now() - renderStart;
          void effectsStart;
          if (this.playing && since >= this.frameMs) this.advance();
        }
        global.requestAnimationFrame(step);
      };
      global.requestAnimationFrame(step);
    }

    trailFromFrame() {
      const frame = this.world && this.world.frame;
      if (!frame) return [];
      const trails = [];
      for (const move of frame.moved || []) {
        if (move.from.x === move.to.x && move.from.y === move.to.y) continue;
        trails.push({ from: move.from, to: move.to });
      }
      return trails;
    }

    /** Advance one frame: seek in record mode, settle a round in live mode. */
    async advance() {
      const world = this.world;
      // `stepping` is the duplicate-step exclusion: one settle at a time.
      if (!world || this.busy || this.stepping) return;
      if (world.index < world.maxIndex) {
        world.index += 1;
        this.enterFrame();
        return;
      }
      if (world.done) {
        this.stop();
        this.showResult(world.result());
        return;
      }
      if (world.mode !== 'live') {
        this.stop();
        this.panel.toast('这是已录制的回放，不能在末尾继续推进。点「重置」或在「请求/响应」里单步。', 'warn');
        return;
      }
      await this.liveStep();
    }

    /**
     * Jump to an already recorded frame (scrub / step back / jump).
     * Animates from the previous committed position when the frame follows the
     * one currently shown, and its effects replay once per visit.
     */
    seek(index) {
      const world = this.world;
      if (!world || this.busy) return;
      this.stop();
      if (!Number.isFinite(index)) return;
      // Jumping is a user decision: a settle still in flight must not pull the
      // view forward again when it lands.
      this.invalidateStep();
      const target = U.clamp(Math.trunc(index), 0, world.maxIndex);
      const from = world.index;
      const animate = target === from + 1;
      // Replaying a frame while paused still shows what happened in it: the
      // events are real, the animation is just re-run for inspection.
      const shouldSpawn = target !== this.effectsSpawnedFor;
      if (!animate || shouldSpawn) this.effects.clear();
      world.index = target;
      this.applyCurrentFrame(animate, shouldSpawn);
    }

    applyCurrentFrame(animate, spawnEffects) {
      const world = this.world;
      if (!world) return;
      const frame = world.frame;
      this.bannerHtml = '';
      world.applyFrame(world.index, { instant: !animate });
      this.frameStartedAt = performance.now();
      this.walkProgress = animate ? 0 : 1;
      this.executedCommands = (frame && frame.executed) || {};
      this.previewCommands = (frame && frame.commands) || {};
      if (frame && (spawnEffects || (animate && this.effectsSpawnedFor !== world.index))) {
        this.effects.spawnForFrame(world, frame, this.frameMs, {
          tile: HW.BASE_TILE,
          height: world.state.mapInfo.height,
          spawnHits: true,
        });
        this.effectsSpawnedFor = world.index;
        for (const id of (frame.skipped || []).slice(0, 3)) {
          this.effects.failed(world, id, HW.BASE_TILE, world.state.mapInfo.height);
        }
      }
      this.panel.executedList(world, frame);
      this.panel.previewList(world, this.previewCommands);
      this.panel.updateHud(world, this.statusInfo());
      this.syncTimeline(this.playing);
      this.panel.renderEvents(world, true);
      this.panel.renderDiagnostics(world, this.diagInfo());
      this.panel.renderTasks(world, this.diagInfo());
      this.panel.renderTwoMatch(this.twoMatch);
      if (this.selected) {
        this.selected = world.actors.find((actor) => actor.key === this.selected.key) || null;
        this.renderer.selected = this.selected;
        this.panel.inspect(this.selected ? { type: 'actor', data: this.selected } : null);
      }
      this.panel.setRequest(JSON.stringify(world.state, null, 2));
      this.panel.setResponse(JSON.stringify({ roleCommandMap: this.previewCommands }, null, 2), '记录中的下一轮指令；回放不重新调用策略。');
      this.refreshReplayTools();
      this.panel.banner(this.bannerHtml || '');
      if (world.done && world.index === world.maxIndex) this.showResult(world.result());
      else this.panel.setMode(world.mode, `seed ${world.seed}`);
    }

    enterFrame() {
      this.applyCurrentFrame(true);
      if (this.follow) {
        const station = this.world.station();
        if (station) this.renderer.centerOnActor(this.world, station);
      }
      if (this.world.done && this.world.index === this.world.maxIndex) this.stop();
    }

    /**
     * Settle one live round. This is the routine path, so it never touches the
     * blocking busy state: locking the transport on every round is what makes the
     * controls flash. One settle at a time (`stepping`), and a response that
     * belongs to a scene the user has already replaced is dropped.
     */
    async liveStep() {
      const world = this.world;
      if (!world || this.busy || this.stepping) return;
      const token = this.generation;
      const index = world.index;
      this.stepping = true;
      this.stepToken = token;
      try {
        this.panel.setStepPending(true);
        const started = performance.now();
        const result = await this.postJson('/debug/step', world.state);
        if (this.staleResponse(world, token, index)) return;
        this.waitingLLM = Boolean(result.pending);
        if (result.pending) {
          this.frameStartedAt = performance.now() + 500;
          return;
        }
        this.timings.step = performance.now() - started;
        world.pushStep(result);
        this.applyCurrentFrame(true);
        this.panel.setRequest(JSON.stringify(world.state, null, 2));
        this.panel.setResponse(JSON.stringify({
          roleCommandMap: this.previewCommands,
          done: result.done,
        }, null, 2), '本地 /debug/step：state + frame + 下一轮 roleCommandMap。');
        if (this.follow) {
          const station = world.station();
          if (station) this.renderer.centerOnActor(world, station);
        }
        if (world.done) {
          this.stop();
          this.showResult(world.result());
        }
      } catch (error) {
        if (this.staleResponse(world, token, index)) return;
        this.stop();
        this.panel.status('结算失败');
        this.panel.toast(`本地结算失败：${error.message}。对局已暂停，可重试「单步」或「重置」。`, 'error');
      } finally {
        if (this.stepToken === token) {
          this.stepping = false;
          this.stepToken = null;
          this.panel.setStepPending(false);
          if (this.world) this.panel.updateHud(this.world, this.statusInfo());
        }
      }
    }

    async refreshPreview() {
      if (!this.world) return;
      const world = this.world;
      const index = world.index;
      const token = this.generation;
      if (world.mode === 'record') return;
      try {
        const response = await this.postJson('/', this.world.state);
        if (this.staleResponse(world, token, index)) return;
        this.previewCommands = response.roleCommandMap || {};
        this.panel.previewList(this.world, this.previewCommands);
        this.panel.setResponse(JSON.stringify(response, null, 2),
          '根路径 POST（官方兼容入口）的真实响应。');
        if (HW.experience) HW.experience.update(world);
      } catch (error) {
        if (this.staleResponse(world, token, index)) return;
        this.panel.previewList(this.world, {});
        this.panel.setResponse(`请求失败：${error.message}`, '根路径 POST 请求失败。');
        this.panel.toast(`策略请求失败：${error.message}`, 'error');
      }
    }

    showResult(result) {
      if (!result.done) return;
      const reason = result.reason || '对局结束';
      this.bannerHtml = `<b>对局结束</b> · ${reason}<br>`
        + `回合 ${result.round} · 天数 D${result.day} · 本地分数 ${result.score} · 击杀 ${result.kills} · 金币 ${result.gold}<br>`
        + `<small>这是本地模拟结果，不是官方成绩。</small>`;
      this.panel.banner(this.bannerHtml, result.station && result.station.dead ? '' : 'ok');
      this.panel.setMode('done', `第 ${result.round} 回合`);
    }

    /* ------------------------------------------------------------ input */
    bind() {
      const on = (id, event, handler) => document.getElementById(id).addEventListener(event, handler);
      on('newmatch', 'click', () => this.startNewMatch());
      on('empty-new', 'click', () => this.startNewMatch());
      on('llm-demo', 'click', () => this.startLLMDemo());
      on('debug-toggle', 'click', () => {
        if (document.getElementById('dev').classList.contains('collapsed')) this.openDebug();
        else this.panel.collapse(true);
      });
      on('view-events', 'click', () => this.openDebug('events'));
      on('selection-details', 'click', () => this.openDebug('inspect'));
      on('save-replay', 'click', () => this.exportRecord());
      on('twomatch-start', 'click', () => this.startTwoMatch());
      on('twomatch-stop', 'click', () => this.stopTwoMatch());
      on('preset', 'click', () => {
        const preset = PRESETS[this.presetIndex % PRESETS.length];
        this.presetIndex += 1;
        document.getElementById('seed').value = String(preset.seed);
        document.getElementById('side').value = preset.side;
        document.getElementById('pressure').value = String(preset.pressure);
        this.startNewMatch(preset);
      });
      on('play', 'click', () => this.playing ? this.stop() : this.play());
      on('pause', 'click', () => this.stop());
      on('step', 'click', () => { this.stop(); this.advance(); });
      on('stepback', 'click', () => this.stepBack());
      on('reset', 'click', () => this.reset());
      on('record', 'click', () => this.loadSeries(600));
      on('recordfull', 'click', () => this.loadSeries(0));
      on('recording-cancel', 'click', () => this.cancelRecording());
      on('recording-retry', 'click', () => this.watchRecording());
      on('import-replay', 'click', () => document.getElementById('replay-file').click());
      on('replay-file', 'change', (event) => this.importFile(event));
      on('event-prev', 'click', () => this.jumpEvent(-1));
      on('event-next', 'click', () => this.jumpEvent(1));
      on('event-kind', 'change', () => this.refreshReplayTools(true));
      on('seek-go', 'click', () => this.commitSeekDraft());
      // In-progress input: frame updates must not overwrite a frame number the
      // user is still typing, nor a timeline thumb that is being dragged. The
      // draft survives blur/change — only Jump, Enter, Escape or a new scene end it.
      on('seek-frame', 'input', () => { this.seekDraft = true; });
      on('seek-frame', 'keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); this.commitSeekDraft(); }
        else if (event.key === 'Escape') { event.preventDefault(); this.discardSeekDraft(); }
      });
      on('speed', 'change', (event) => { this.frameMs = Number(event.target.value); this.syncTimeline(); });
      on('scrub', 'input', (event) => this.seek(Number(event.target.value)));
      on('scrub', 'pointerdown', () => { this.scrubActive = true; });
      on('scrub', 'pointerup', () => this.endScrub());
      on('scrub', 'pointercancel', () => this.endScrub());
      on('scrub', 'keydown', () => { this.scrubActive = true; });
      on('scrub', 'keyup', () => this.endScrub());
      on('scrub', 'blur', () => this.endScrub());
      on('scrub', 'change', () => this.endScrub());
      on('fit', 'click', () => { this.fitMode = true; this.world && this.renderer.fit(this.world); this.follow = false; });
      on('zoomin', 'click', () => { this.fitMode = false; this.renderer.zoomAt(this.renderer.viewport.width / 2, this.renderer.viewport.height / 2, 1.25); });
      on('zoomout', 'click', () => { this.fitMode = false; this.renderer.zoomAt(this.renderer.viewport.width / 2, this.renderer.viewport.height / 2, 0.8); });
      on('follow', 'click', () => {
        this.follow = !this.follow;
        this.fitMode = false;
        document.getElementById('follow').classList.toggle('primary', this.follow);
        if (this.follow && this.world) {
          const station = this.world.station();
          this.renderer.camera.scale = Math.max(this.renderer.camera.scale, 0.8);
          if (station) this.renderer.centerOnActor(this.world, station);
        }
      });
      on('ribbon-toggle', 'click', () => {
        const note = document.querySelector('.diff-note');
        this.openDebug('rules');
        if (note) note.scrollIntoView({ behavior: 'smooth', block: 'center' });
      });
      on('collapse', 'click', () => this.panel.collapse(!document.getElementById('dev').classList.contains('collapsed')));
      for (const tab of document.querySelectorAll('.tab')) {
        tab.addEventListener('click', () => this.panel.activateTab(tab.dataset.tab));
      }
      on('ev-autoscroll', 'change', (event) => { this.panel.autoScroll = event.target.checked; });

      on('opt-grid', 'change', (event) => { this.renderer.options.grid = event.target.checked; });
      on('opt-ranges', 'change', (event) => { this.showAllRanges = event.target.checked; });
      on('opt-preview', 'change', (event) => { this.renderer.options.preview = event.target.checked; });
      on('opt-executed', 'change', (event) => { this.renderer.options.executed = event.target.checked; });

      on('sample', 'click', () => this.loadSample());
      on('empty-sample', 'click', () => this.loadSample());
      on('apply-json', 'click', () => this.applyJson());
      on('decide-json', 'click', () => this.decideJson());
      on('round', 'change', () => this.changeRound());
      on('export', 'click', () => this.exportRecord());
      on('import', 'click', () => document.getElementById('file').click());
      on('file', 'change', (event) => this.importFile(event));
      on('input-json', 'input', () => {
        this.panel.markRequestEdited();
        this.panel.setResponse('请求已修改，点「应用到画面」或「只跑策略」。', '尚未请求。');
      });

      this.bindCanvas();
      this.bindSplitter();
      const observer = new ResizeObserver(() => this.resize());
      observer.observe(document.getElementById('stage-canvas'));
      global.addEventListener('resize', () => this.resize());
      // A drag can end outside the slider; release the scrub lock wherever it ends.
      global.addEventListener('pointerup', () => this.endScrub());
      document.addEventListener('keydown', (event) => this.onKey(event));
      document.addEventListener('click', (event) => {
        for (const menu of document.querySelectorAll('.menu[open]')) {
          if (!menu.contains(event.target) || event.target.closest('button')) menu.open = false;
        }
      });
      document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') for (const menu of document.querySelectorAll('.menu[open]')) menu.open = false;
      });
    }

    bindCanvas() {
      const canvas = document.getElementById('map');
      canvas.addEventListener('pointerdown', (event) => {
        // Pointer capture can fail (synthetic events, exotic devices); the drag
        // must still work, so it is best-effort only.
        try { canvas.setPointerCapture(event.pointerId); } catch (error) { void error; }
        this.dragging = { x: event.clientX, y: event.clientY, moved: 0, button: event.button };
        canvas.classList.add('dragging');
      });
      canvas.addEventListener('pointermove', (event) => {
        const rect = canvas.getBoundingClientRect();
        const sx = event.clientX - rect.left;
        const sy = event.clientY - rect.top;
        if (this.dragging && (this.dragging.button === 0 || this.dragging.button === 1)) {
          const dx = event.clientX - this.dragging.x;
          const dy = event.clientY - this.dragging.y;
          if (Math.abs(dx) + Math.abs(dy) > 2) {
            this.dragging.moved += Math.abs(dx) + Math.abs(dy);
            this.fitMode = false;
            this.renderer.panBy(dx, dy);
            this.dragging.x = event.clientX;
            this.dragging.y = event.clientY;
          }
        }
        this.updateHover(sx, sy);
      });
      canvas.addEventListener('pointerup', (event) => {
        canvas.classList.remove('dragging');
        const dragging = this.dragging;
        this.dragging = null;
        if (!dragging || dragging.moved > 6) return;
        const rect = canvas.getBoundingClientRect();
        this.pickAt(event.clientX - rect.left, event.clientY - rect.top);
      });
      canvas.addEventListener('pointerleave', () => {
        this.hover = null;
        document.getElementById('tooltip').hidden = true;
      });
      canvas.addEventListener('wheel', (event) => {
        event.preventDefault();
        this.fitMode = false;
        const rect = canvas.getBoundingClientRect();
        this.renderer.zoomAt(event.clientX - rect.left, event.clientY - rect.top, event.deltaY < 0 ? 1.12 : 0.89);
      }, { passive: false });
      canvas.addEventListener('dblclick', () => { this.fitMode = true; this.world && this.renderer.fit(this.world); });
    }

    onKey(event) {
      if (document.getElementById('map-guide')?.open) return;
      if (event.target.matches('input, textarea, select')) return;
      if (event.code === 'Space') {
        event.preventDefault();
        if (this.playing) this.stop(); else this.play();
      } else if (event.code === 'ArrowRight') {
        event.preventDefault(); this.stop(); this.advance();
      } else if (event.code === 'ArrowLeft') {
        event.preventDefault(); this.stepBack();
      } else if (event.key === 'f' || event.key === 'F') {
        this.fitMode = true;
        this.world && this.renderer.fit(this.world);
      }
    }

    updateHover(sx, sy) {
      const world = this.world;
      const tooltip = document.getElementById('tooltip');
      if (!world) return;
      const escape = (value) => String(value).replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
      const cell = this.renderer.screenToCell(sx, sy, world);
      this.hoverCell = cell;
      const actor = this.renderer.pick(world, sx, sy);
      this.hover = actor;
      const stage = document.getElementById('stage-canvas').getBoundingClientRect();
      if (actor) {
        const ratio = U.clamp(actor.health / (actor.maxHealth || 1), 0, 1);
        const lines = [
          `<div class="row"><span>${actor.owner === 'own' ? '我方' : (actor.owner === 'enemy' ? '敌方' : '机器人')}</span><b>${escape(actor.label)}</b></div>`,
          `<div class="row"><span>ID</span><span>${escape(actor.id)}</span></div>`,
          `<div class="row"><span>坐标</span><span>${U.cellLabel(actor.pos)}</span></div>`,
          `<div class="row"><span>血量</span><span>${actor.health} / ${actor.maxHealth} (${(ratio * 100).toFixed(0)}%)</span></div>`,
        ];
        if (HW.OFFICIAL.towerTypes.includes(actor.kind)) {
          lines.push(`<div class="row"><span>等级/射程</span><span>Lv${actor.level} · ${HW.rangeOf(actor.kind, actor.level)}</span></div>`);
          lines.push(`<div class="row"><span>冷却</span><span>${actor.cooldown > 0 ? `${actor.cooldown} 回合` : '就绪'}</span></div>`);
        }
        if (actor.capacity) {
          lines.push(`<div class="row"><span>背包</span><span>${actor.backpack.length}/${actor.capacity}</span></div>`);
        }
        if (actor.unmodelled) lines.push('<div class="row"><span>提示</span><span>未建模类型，已降级显示</span></div>');
        tooltip.innerHTML = lines.join('');
        tooltip.style.left = `${U.clamp(sx + 16, 6, stage.width - 250)}px`;
        tooltip.style.top = `${U.clamp(sy + 14, 6, stage.height - 110)}px`;
        tooltip.hidden = false;
      } else {
        const zone = this.renderer.zoneAt(world, cell);
        if (zone) {
          tooltip.innerHTML = `<div class="row"><span>中立点</span><b>${escape(zone.label)}</b></div>`
            + `<div class="row"><span>坐标</span><span>${U.cellLabel(zone.pos)}</span></div>`
            + `<div class="row"><span>占格</span><span>${zone.size}</span></div>`;
          tooltip.style.left = `${U.clamp(sx + 16, 6, stage.width - 250)}px`;
          tooltip.style.top = `${U.clamp(sy + 14, 6, stage.height - 110)}px`;
          tooltip.hidden = false;
        } else {
          tooltip.hidden = true;
        }
      }
    }

    pickAt(sx, sy) {
      const world = this.world;
      if (!world) return;
      const actor = this.renderer.pick(world, sx, sy);
      if (actor) {
        this.selected = actor;
        this.renderer.selected = actor;
        this.panel.inspect({ type: 'actor', data: actor });
        if (HW.experience) HW.experience.selection(actor);
        return;
      }
      const cell = this.renderer.screenToCell(sx, sy, world);
      const zone = this.renderer.zoneAt(world, cell);
      this.selected = null;
      this.renderer.selected = null;
      this.panel.inspect(zone ? { type: 'zone', data: zone } : null);
      if (HW.experience) {
        HW.experience.selection(null);
        if (zone) {
          document.getElementById('selection-card').hidden = false;
          document.getElementById('selection-title').textContent = zone.label;
          document.getElementById('selection-text').textContent = `中立地点，位置 (${zone.pos.x}, ${zone.pos.y})。点击详细字段查看更多信息。`;
        }
      }
    }

    /* ------------------------------------------------------ transport */
    /** Timeline readout that never fights input the user is still editing. */
    syncTimeline(playing) {
      const world = this.world || this.placeholderWorld();
      this.panel.updateTimeline(world, playing == null ? this.playing : playing, {
        preserveScrub: this.scrubActive,
      });
    }

    /**
     * Apply the frame number the user typed. The value is read before the draft
     * is released, so blur (which fires before the Jump click) cannot replace it
     * with the frame that happens to be displayed.
     */
    commitSeekDraft() {
      const node = document.getElementById('seek-frame');
      const target = Number(node.value);
      this.seekDraft = false;
      this.seek(target);
    }

    /** Abandon a typed frame number and show the frame that is really displayed. */
    discardSeekDraft() {
      if (!this.seekDraft) return;
      this.seekDraft = false;
      if (this.world) this.panel.syncSeekFrame(this.world, false);
    }

    /** Release the timeline drag lock and resync the thumb to the real frame. */
    endScrub() {
      if (!this.scrubActive) return;
      this.scrubActive = false;
      this.syncTimeline();
    }

    play() {
      if (this.busy) return;
      if (!this.world) { this.panel.toast('先新建对局或载入示例。', 'warn'); return; }
      if (this.world.done && this.world.index >= this.world.maxIndex) {
        this.panel.toast('对局已结束，点「重置」重新开始。', 'warn');
        return;
      }
      this.playing = true;
      this.frameStartedAt = performance.now() - this.frameMs;
      this.panel.updateHud(this.world, this.statusInfo());
      this.syncTimeline(true);
      if (this.world.index === 0) this.advance();
    }

    stop() {
      if (!this.playing) return;
      this.playing = false;
      if (this.world) {
        this.panel.updateHud(this.world, this.statusInfo());
        this.syncTimeline(false);
      }
    }

    stepBack() {
      this.stop();
      const world = this.world;
      if (!world || world.index === 0) return;
      this.seek(world.index - 1);
    }

    async reset() {
      if (this.busy) return;
      this.stop();
      if (!this.world) { await this.newMatch(); return; }
      this.effects.clear();
      this.panel.banner('');
      if (this.world.mode === 'record' || this.world.mode === 'live') {
        this.world.applyFrame(0, { instant: true });
        this.afterWorldChange();
        this.applyCurrentFrame(false, false);
        this.panel.status('已回到初始帧');
        this.panel.setMode(this.world.mode, `地图 ${this.world.seed} · 已记录 ${this.world.frameCount} 轮`);
        return;
      }
      await this.newMatch({
        seed: this.world.seed, side: this.world.side, pressure: this.world.pressure,
      });
    }

    /* ------------------------------------------------------------ io */
    async loadSample() {
      if (this.busy) return;
      this.stop();
      try {
        const response = await fetch('/sample');
        if (!response.ok) throw new Error(`示例载入失败 ${response.status}`);
        const text = await response.text();
        JSON.parse(text);
        this.sampleState = JSON.parse(text);
        // An explicit load replaces whatever was typed in the request box.
        this.panel.setRequest(text, true);
        this.applyJson(true);
        this.panel.setMode('sample');
        this.panel.status('官方示例已载入');
        this.panel.toast('已载入 docs/request.txt 官方示例（单回合调试，不是完整对局）。', null);
        this.openDebug('io');
      } catch (error) {
        this.panel.toast(`示例载入失败：${error.message}`, 'error');
      }
    }

    applyJson(silent) {
      if (this.busy) return false;
      this.stop();
      try {
        const parsed = JSON.parse(document.getElementById('input-json').value);
        const problems = this.validate(parsed);
        if (problems.length) throw new Error(problems[0]);
        const world = new HW.World();
        world.seed = (parsed._demo && parsed._demo.seed) || Number(document.getElementById('seed').value) || 1;
        world.side = (parsed.teamOur && parsed.teamOur.type) || 'challenger';
        world.pressure = (parsed._demo && parsed._demo.pressure) || 1;
        world.states = [parsed];
        world.frames = [];
        world.index = 0;
        world.mode = 'sample';
        world.positionsCache = [HW.viewModel.positionMap(parsed)];
        world.applyFrame(0, { instant: true });
        this.world = world;
        this.invalidateStep();
        this.effects.clear();
        this.effectsSpawnedFor = -1;
        this.panel.empty(false);
        this.panel.setMode('sample');
        this.panel.status('已应用 JSON');
        this.panel.acceptRequestEdit();
        this.applyCurrentFrame(false, false);
        this.renderer.fit(this.world);
        if (!silent) this.panel.toast('请求 JSON 已应用到画面（单帧，不含本地生命周期）。', null);
        this.refreshPreview();
        return true;
      } catch (error) {
        this.panel.toast(`请求 JSON 无效：${error.message}`, 'error');
        this.panel.status('JSON 无效');
        this.invalidateStep();
        // Never keep painting a scene the user just rejected: show the empty
        // state instead, so a stale map cannot be mistaken for the new input.
        if (this.world) {
          this.world = null;
          this.effects.clear();
          this.renderer.selected = null;
          this.selected = null;
          this.panel.inspect(null);
          this.panel.updateHud(this.placeholderWorld(), { label: 'JSON 无效', detail: '画面已清空，修正后重新应用' });
          this.panel.setMode('idle');
          this.panel.empty(true, `请求 JSON 无效：${error.message}`);
        }
        return false;
      }
    }

    async decideJson() {
      try {
        const parsed = JSON.parse(document.getElementById('input-json').value);
        const problems = this.validate(parsed);
        if (problems.length) throw new Error(problems[0]);
        this.panel.status('请求策略…');
        const response = await this.postJson('/', parsed);
        this.panel.setResponse(JSON.stringify(response, null, 2), '根路径 POST（官方兼容入口）。');
        this.panel.previewList(this.world || this.placeholderWorld(), response.roleCommandMap || {});
        this.panel.activateTab('plan');
        this.panel.status('策略已返回');
        if (this.world && this.world.mode === 'sample') {
          this.previewCommands = response.roleCommandMap || {};
          this.panel.previewList(this.world, this.previewCommands);
        }
      } catch (error) {
        this.panel.setResponse(`请求失败：${error.message}`, '请求失败。');
        this.panel.toast(`策略请求失败：${error.message}`, 'error');
        this.panel.status('请求失败');
      }
    }

    changeRound() {
      try {
        const parsed = JSON.parse(document.getElementById('input-json').value);
        parsed.roundNo = Number(document.getElementById('round').value);
        const problems = this.validate(parsed);
        if (problems.length) throw new Error(problems[0]);
        document.getElementById('input-json').value = JSON.stringify(parsed, null, 2);
        this.applyJson(true);
        this.panel.status('回合已切换');
        this.panel.toast('只改了观测里的 roundNo（昼夜输入），没有改任何规则。', null);
      } catch (error) {
        this.panel.toast(`回合修改失败：${error.message}`, 'error');
      }
    }

    /** Local shape checks only. The judge stays the authority on the protocol. */
    validate(payload) {
      const problems = [];
      if (!payload || typeof payload !== 'object') return ['请求必须是 JSON 对象'];
      if (!payload.mapInfo) problems.push('缺少 mapInfo');
      if (!payload.teamOur) problems.push('缺少 teamOur');
      if (problems.length) return problems;
      const info = payload.mapInfo;
      if (!Number.isInteger(info.width) || !Number.isInteger(info.height)
        || info.width < 1 || info.height < 1 || info.width > 200 || info.height > 200) {
        problems.push('地图宽高必须是 1–200 的整数');
      }
      if (!Number.isInteger(payload.roundNo) || payload.roundNo < 1 || payload.roundNo > HW.OFFICIAL.maxRounds) {
        problems.push(`roundNo 必须是 1–${HW.OFFICIAL.maxRounds} 的整数`);
      }
      const groups = [info.zones || [], payload.teamOur.roles || [],
        (payload.teamEnemy && payload.teamEnemy.roles) || [],
        (payload.robot && payload.robot.roles) || []];
      for (const list of groups) {
        if (!Array.isArray(list)) { problems.push('区域与角色必须是数组'); continue; }
        for (const unit of list) {
          if (!unit || !unit.pos || !Number.isInteger(unit.pos.x) || !Number.isInteger(unit.pos.y)) {
            problems.push('存在缺少合法坐标的单位'); break;
          }
          if (unit.pos.x < 0 || unit.pos.x >= info.width || unit.pos.y < 0 || unit.pos.y >= info.height) {
            problems.push('存在超出地图范围的单位坐标'); break;
          }
        }
      }
      if (!(payload.teamOur.roles || []).length) problems.push('我方角色为空：这是一个空场景，无法推进对局');
      return problems;
    }

    async exportRecord() {
      const world = this.world;
      if (!world) { this.panel.toast('还没有可导出的内容。', 'warn'); return; }
      this.stop();
      try {
      const payload = await HW.Replay.pack(world);
      const blob = new Blob([JSON.stringify(payload)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `futurewar-local-seed${world.seed}-${world.side}-r${world.roundNo}.json`;
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      this.panel.toast(`已保存 ${world.frameCount} 回合完整录像，可使用「打开录像」离线复盘。`, null);
      } catch (error) { this.panel.toast(`保存录像失败：${error.message}`, 'error'); }
    }

    async importFile(event) {
      const file = event.target.files && event.target.files[0];
      event.target.value = '';
      if (!file) return;
      let ownsBusy = false;
      try {
        if (this.busy) throw new Error('请等待当前操作完成');
        this.stop();
        this.busy = true;
        ownsBusy = true;
        this.panel.setBusy(true);
        this.panel.status('读取并校验文件…');
        if (file.size > 128 * 1024 * 1024) throw new Error('文件超过 128 MB');
        const text = await file.text();
        const parsed = JSON.parse(text);
        if (parsed.kind === HW.Replay.KIND) {
          this.openRecording(await HW.Replay.unpack(parsed));
          this.panel.toast(`已打开 ${file.name}，共 ${this.world.frameCount} 回合；校验通过。`);
          return;
        }
        if (Array.isArray(parsed.frames)) {
          this.openRecording(parsed);
          this.panel.toast('已打开旧版完整录像；此格式未提供文件校验。', 'warn');
          return;
        }
        if (parsed.kind === 'competition-hw-local-record')
          throw new Error('旧版导出只有单帧摘要，无法恢复整场；请重新录制并保存完整录像');
        const problems = this.validate(parsed);
        if (problems.length) throw new Error(problems[0]);
        // An imported file replaces whatever was typed in the request box.
        this.panel.setRequest(text, true);
        this.busy = false;
        if (this.applyJson(true)) this.panel.toast(`已导入 ${file.name} 作为单帧请求。`, null);
      } catch (error) {
        this.panel.toast(`导入失败：${error.message}`, 'error');
      } finally {
        if (ownsBusy) {
          this.busy = false; this.panel.setBusy(false);
          if (this.world) this.panel.updateHud(this.world, this.statusInfo());
        }
      }
    }

    refreshReplayTools(force) {
      const world = this.world;
      if (!world) return;
      const filter = document.getElementById('event-kind').value;
      if (force || this.markerWorld !== world || this.markerCount !== world.frameCount) {
        this.markers = HW.Replay.markers(world);
        this.markerWorld = world;
        this.markerCount = world.frameCount;
        const box = document.getElementById('event-markers');
        box.replaceChildren();
        const names = { skip: '失败指令', death: '阵亡', spawn: '波次', task: '任务/宝藏', phase: '昼夜切换' };
        for (const marker of this.markers) {
          if (filter !== 'all' && !marker.types.includes(filter)) continue;
          const button = document.createElement('button');
          button.type = 'button';
          button.className = `event-marker ${marker.types[0]}`;
          button.style.left = `${marker.index / Math.max(1, world.maxIndex) * 100}%`;
          button.title = `已结算回合 ${marker.round} · ${marker.types.map((t) => names[t]).join(' / ')}`;
          button.setAttribute('aria-label', button.title);
          button.addEventListener('click', () => this.seek(marker.index));
          box.append(button);
        }
      }
      const failures = world.frames.reduce((sum, frame) => sum + (frame.skipped || []).length, 0);
      document.getElementById('replay-summary').textContent = `${world.frameCount} 已记录回合 · ${failures} 条失败指令 · ${this.markers.length} 关键帧`;
      const metadata = world.metadata || {};
      document.getElementById('replay-summary').title = `规则 ${metadata.rulesBaseline || '未记录'}\n源码 SHA256 ${metadata.sourceSha256 || '未记录'}\n${(metadata.limitations || []).join('；')}`;
      this.panel.syncSeekFrame(world, this.seekDraft);
      for (const [id, direction] of [['event-prev', -1], ['event-next', 1]])
        document.getElementById(id).disabled = HW.Replay.nextMarker(this.markers, world.index, direction, filter) === null;
    }

    jumpEvent(direction) {
      if (!this.world || this.busy) return;
      const target = HW.Replay.nextMarker(this.markers, this.world.index, direction, document.getElementById('event-kind').value);
      if (target !== null) this.seek(target);
    }

    resize() {
      const box = document.getElementById('stage-canvas').getBoundingClientRect();
      const width = Math.max(320, box.width);
      const height = Math.max(200, box.height);
      const before = this.world ? { x: this.renderer.camera.x, y: this.renderer.camera.y } : null;
      // Identical sizes are a no-op: rewriting canvas.width/height would clear the
      // bitmap and flash the map on every ResizeObserver tick.
      if (!this.renderer.setSize(width, height) || !this.world) return;
      if (this.fitMode) this.renderer.fit(this.world);
      else if (before) this.renderer.camera = { x: before.x, y: before.y, scale: this.renderer.camera.scale };
    }

    /**
     * Drag the divider to trade console height against the debug panel. The
     * panel lives in the console, so this only resizes console content; the map
     * column keeps its size and the canvas is never re-created by a drag.
     */
    bindSplitter() {
      const splitter = document.getElementById('splitter');
      const dev = document.getElementById('dev');
      let dragging = false;
      let startY = 0;
      let startHeight = 0;
      const apply = (clientY) => {
        const limit = Math.max(160, window.innerHeight - 120);
        const height = U.clamp(startHeight - (clientY - startY), 120, limit);
        dev.style.height = `${height}px`;
        this.resize();
      };
      const onMove = (event) => { if (dragging) { event.preventDefault(); apply(event.clientY); } };
      const stop = () => {
        dragging = false;
        splitter.classList.remove('active');
        document.body.style.userSelect = '';
      };
      splitter.addEventListener('pointerdown', (event) => {
        dragging = true;
        startY = event.clientY;
        startHeight = dev.getBoundingClientRect().height || 340;
        try { splitter.setPointerCapture(event.pointerId); } catch (error) { void error; }
        splitter.classList.add('active');
        document.body.style.userSelect = 'none';
      });
      splitter.addEventListener('pointermove', onMove);
      splitter.addEventListener('pointerup', stop);
      splitter.addEventListener('pointercancel', stop);
      splitter.addEventListener('keydown', (event) => {
        const current = dev.getBoundingClientRect().height || 340;
        if (event.key === 'ArrowUp') { dev.style.height = `${current + 24}px`; this.resize(); }
        else if (event.key === 'ArrowDown') { dev.style.height = `${Math.max(120, current - 24)}px`; this.resize(); }
        else return;
        event.preventDefault();
      });
    }
  }

  HW.App = App;
  global.addEventListener('DOMContentLoaded', () => {
    const app = new App();
    HW.app = app;
    app.boot().catch((error) => {
      const panel = app.panel;
      panel.toast(`初始化失败：${error.message}`, 'error');
      panel.empty(true, '页面初始化失败，请刷新或检查服务日志。');
    });
  });
}(window));
