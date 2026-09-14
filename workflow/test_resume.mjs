// Fake-native protocol test for workflow/dsh_steering_bridge.mjs.
//
// It drives the real bridge module against the real
// HarnessSdkJsonRpcServer from the installed DSH package, with a fake native
// `ctx.agents` registry. That asserts, without a model or a network call:
//   * competition/resume calls ctx.agents.resume with the ORIGINAL session id;
//   * ctx.agents.create is never called for a resumed session;
//   * the resumed handle is attached to the SDK server, so a later
//     session/prompt follows the ordinary session.event/status channel;
//   * a resume without native persistence, or for an already-live agent, fails
//     loudly instead of creating a fresh same-id session.
//
// Run:  node workflow/test_resume.mjs
// Resolves the installed @deepseek-ai/dsh/package.json from
// COMPETITION_DSH_PACKAGE_JSON (which dsh_runner.launch sets for workers), or
// from the `dsh` executable on PATH when the variable is not set.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { pathToFileURL } from 'node:url';

function packageJson() {
  const configured = process.env.COMPETITION_DSH_PACKAGE_JSON;
  if (configured) {
    assert.ok(existsSync(configured), `COMPETITION_DSH_PACKAGE_JSON does not exist: ${configured}`);
    return configured;
  }
  const shim = execFileSync(process.platform === 'win32' ? 'where.exe' : 'which',
    [process.platform === 'win32' ? 'dsh.ps1' : 'dsh'], { encoding: 'utf8' }).split(/\r?\n/)[0].trim();
  const candidate = join(dirname(dirname(shim)), 'node_modules', '@deepseek-ai', 'dsh', 'package.json');
  assert.ok(existsSync(candidate), `cannot locate @deepseek-ai/dsh from ${shim}`);
  return candidate;
}

const require = createRequire(packageJson());
const { HarnessSdkJsonRpcServer } = await import(
  pathToFileURL(require.resolve('@deepseek-ai/dsh-sdk-jsonrpc-server')).href);
const bridge = await import(new URL('./dsh_steering_bridge.mjs', import.meta.url).href);

const SESSION = 'competition-strategy-1af920579f54486caf5d8a623c45343a';
const CWD = 'D:/Research_vault/work/projects/Code_HW/.workflow/worktrees/ds-strategy';
const MODEL = { provider: 'deepseek-official', model: 'deepseek-flash', reasoningEffort: 'max' };

/** Minimal transport peer: records notifications and exposes the request handler. */
class FakeTransport {
  constructor() { this.notifications = []; this.handler = undefined; this.started = false; }
  start() { this.started = true; }
  close() { this.started = false; }
  onRequest(handler) { this.handler = handler; }
  notify(method, params) { this.notifications.push({ method, params }); }
  async flush() {}
  request(method, params) { return this.handler(method, params); }
}

/** A persisted native agent as the resumed registry would publish it. */
function resumedAgent(sessionId, options = {}) {
  const session = { id: sessionId, header: { meta: { cwd: CWD } } };
  const inbox = { nextTurn: [], nextStep: [], append(target, message) { this[target].push(message); } };
  return { id: sessionId, session, options: { ...MODEL, ...options }, status: 'idle', inbox,
           followup(message) { inbox.append('nextTurn', message); },
           steer(message) { inbox.append('nextStep', message); } };
}

async function harness({ persistence = true, live = false, resumeError, agentOptions = {} } = {}) {
  const transport = new FakeTransport();
  const calls = { resume: [], create: [], disposed: [] };
  const agent = resumedAgent(SESSION, agentOptions);
  const registry = new Map();
  if (live) registry.set(SESSION, agent);
  const agents = {
    get(id) { return registry.get(id); },
    async resume(options) {
      calls.resume.push(options);
      if (resumeError) throw new Error(resumeError);
      registry.set(SESSION, agent);
      return { agent, dispose: async () => { registry.delete(SESSION); calls.disposed.push(String(agent.id)); } };
    },
    async create(options) { calls.create.push(options); throw new Error('agents.create must not be called'); }
  };
  const events = [];
  const ctx = {
    agents,
    get(service) {
      if (service === 'sessionPersistence') return persistence ? { open() {} } : undefined;
      if (service === 'loader') return undefined;
      if (service === 'attachments') return undefined;
      return undefined;
    },
    plugin() { return { dispose: async () => {} }; },
    on(kind, listener) { events.push({ kind, listener }); return () => {}; },
    effect(setup) { return setup(); }
  };
  const server = new HarnessSdkJsonRpcServer(ctx, transport, { maxTokensAsSuccess: false });
  // The state a completed `initialize` handshake reaches: the handshake itself
  // needs a live llm adapter, so the fake-native harness sets exactly the
  // fields the installed server and the bridge read.
  Object.assign(server, { cwd: CWD, ...MODEL, initialized: true });
  bridge.apply(ctx, { transport, server });
  // Emit through the native subscription the server installed, so the bridge
  // and the server observe the same session event stream.
  const emit = (kind, type, data) => events.filter((e) => e.kind === kind)
    .forEach((e) => e.listener({ id: SESSION }, { type, data }));
  return { transport, calls, ctx, agent, events, server, emit };
}

const results = [];
async function test(name, body) {
  try { await body(); results.push(`ok   ${name}`); }
  catch (error) { results.push(`FAIL ${name}: ${error.message}`); process.exitCode = 1; }
}

await test('resume uses the original session id and never agents.create', async () => {
  const { transport, calls } = await harness();
  const confirmation = await transport.handler('competition/resume', { resumeSessionId: SESSION });
  assert.equal(calls.resume.length, 1);
  assert.deepEqual(calls.resume[0], { resumeSessionId: SESSION, agentOptions: MODEL });
  assert.equal(calls.create.length, 0);
  assert.equal(confirmation.resumed, true);
  assert.equal(confirmation.sessionId, SESSION);
  assert.equal(confirmation.created, false);
  assert.equal(confirmation.deliveredBy, 'ctx.agents.resume');
  assert.equal(confirmation.model, MODEL.model);
});

await test('a prompt after resume attaches to the resumed agent, not a new one', async () => {
  const { transport, calls, agent } = await harness();
  await transport.handler('competition/resume', { resumeSessionId: SESSION });
  const accepted = await transport.handler('session/prompt',
    { sessionId: SESSION, contentBlocks: [{ type: 'text', text: 'follow-up manifest' }] });
  assert.equal(typeof accepted.messageId, 'string');
  assert.equal(calls.create.length, 0, 'resumed session must not be created');
  assert.equal(calls.resume.length, 1);
  assert.equal(agent.inbox.nextTurn.length, 1, 'the follow-up is queued on the resumed agent');
  assert.equal(agent.inbox.nextTurn[0].content[0].text, 'follow-up manifest');
});

await test('resume fails loudly without native persistence', async () => {
  const { transport, calls } = await harness({ persistence: false });
  await assert.rejects(transport.handler('competition/resume', { resumeSessionId: SESSION }),
    /persistence is not configured/);
  assert.equal(calls.resume.length, 0);
  assert.equal(calls.create.length, 0);
});

await test('resume refuses to replace a live native agent', async () => {
  const { transport, calls } = await harness({ live: true });
  await assert.rejects(transport.handler('competition/resume', { resumeSessionId: SESSION }),
    /already live/);
  assert.equal(calls.resume.length, 0);
  assert.equal(calls.create.length, 0);
});

await test('a missing persisted session is reported, not replaced', async () => {
  const { transport, calls } = await harness({ resumeError: 'session not found: ' + SESSION });
  await assert.rejects(transport.handler('competition/resume', { resumeSessionId: SESSION }),
    /Native resume failed.*session not found/);
  assert.equal(calls.create.length, 0);
  assert.equal(calls.disposed.length, 0);
});

await test('a resumed agent with another model is refused and disposed', async () => {
  const { transport, calls } = await harness({ agentOptions: { model: 'deepseek-pro' } });
  await assert.rejects(transport.handler('competition/resume', { resumeSessionId: SESSION }),
    /model changed/);
  assert.deepEqual(calls.disposed, [SESSION]);
});

await test('resume validates its parameter', async () => {
  const { transport, calls } = await harness();
  await assert.rejects(transport.handler('competition/resume', {}), /resumeSessionId is required/);
  assert.equal(calls.resume.length, 0);
});

await test('an SDK server without the attach seam is reported, not bypassed', async () => {
  const { transport, calls, server } = await harness();
  delete server.sessions;
  await assert.rejects(transport.handler('competition/resume', { resumeSessionId: SESSION }),
    /cannot attach a resumed agent \(missing sessions\)/);
  assert.equal(calls.resume.length, 0);
  assert.equal(calls.create.length, 0);
});

await test('event and steering plumbing is unchanged after resume', async () => {
  const { transport, emit, agent, calls } = await harness();
  await transport.handler('competition/resume', { resumeSessionId: SESSION });
  assert.equal(calls.create.length, 0);
  const capabilities = await transport.handler('competition/capabilities', {});
  assert.equal(capabilities.nativeSteer, true);
  assert.equal(capabilities.nativeResume, true);
  // The resumed turn is announced on the ordinary native session/event stream.
  agent.status = 'running';
  emit('session/event', 'turn/start', { turn: 3 });
  const receipt = await transport.handler('competition/steer',
    { sessionId: SESSION, expectedTurn: 3, requestId: 'r1', text: 'keep going' });
  assert.equal(receipt.accepted, true);
  assert.equal(receipt.delivery, 'next-step');
  assert.equal(receipt.sessionId, SESSION);
  assert.equal(receipt.turn, 3);
  // The same channel reports the message actually entering the native turn.
  agent.inbox.nextTurn.push({ id: receipt.messageId, content: [{ type: 'text', text: 'keep going' }] });
  emit('session/event', 'user/message', { id: receipt.messageId });
  assert.ok(transport.notifications.some((n) => n.method === 'competition.steer_consumed' && n.params.consumed),
    'consumption still rides the native user/message event');
});

console.log(results.join('\n'));
