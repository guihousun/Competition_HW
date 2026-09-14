// Project-local transport adapter. All agent/context behavior stays native DSH.
//
// It replaces only the stdio entry point of the installed SDK JSON-RPC server in
// this one process, and adds three project-local methods on the same channel:
//   competition/capabilities - reports which native controls are available.
//   competition/steer        - posts one message into the live native turn.
//   competition/resume       - re-attaches the SDK server to a persisted native
//                              session through ctx.agents.resume (never
//                              ctx.agents.create, never a hand-built history).
// `config.transport`/`config.server` exist so tests can drive this module with
// the installed server class over a fake transport; production uses process stdio.
// Verified against installed @deepseek-ai/dsh 0.1.5-rc.1:
//   dsh-agent        AgentRegistry.resume(options) -> factory.resume(ownerCtx, options)
//   dsh-agent-loop   agentLoop.resume -> persistence.open(id,'write'), cold read,
//                    appended interruptedTurnClosers, then publication.
//   dsh-sdk-jsonrpc-server  HarnessSdkJsonRpcServer.session/prompt resolves its
//                    per-session record through getOrCreateSession(), which
//                    creates only when its `sessions` map has no record.
// So the resumed handle is attached by placing it in that map before the first
// session/prompt; the natural event stream and steering channel are unchanged.
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const require = createRequire(process.env.COMPETITION_DSH_PACKAGE_JSON);
const native = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-sdk-jsonrpc-server')).href);
const { JsonRpcLineTransport } = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-sdk-protocol')).href);
const { createUserMessage } = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-llm')).href);
export const name = 'competition-steering-server';
export const inject = ['agents', 'sessions', 'loader'];
export const Config = native.Config;

// Fields read from the installed SDK server. A mismatch is reported instead of
// silently falling back to a fresh session.
const ATTACH = ['sessions', 'cwd', 'provider', 'model', 'reasoningEffort', 'handleRequest'];

/** Working directory recorded on the persisted session, when the log carries one. */
function resumedCwd(agent) {
  const cwd = agent?.session?.header?.meta?.cwd;
  return typeof cwd === 'string' ? cwd : undefined;
}

export function apply(ctx, config = {}) {
  const transport = config.transport ?? new JsonRpcLineTransport(process.stdin, process.stdout);
  const server = config.server ?? new native.HarnessSdkJsonRpcServer(ctx, transport, { maxTokensAsSuccess: false });
  const owned = new Set(), turns = new Map(), receipts = new Map(), messages = new Map(), requests = new Map();
  const off = ctx.on('session/event', (session, event) => {
    const sid = String(session.id);
    if (event.type === 'turn/start') turns.set(sid, event.data.turn);
    if (event.type === 'turn/end') turns.delete(sid);
    if (event.type === 'user/message') {
      const receipt = messages.get(`${sid}:${event.data.id}`);
      if (receipt) {
        receipt.consumed = true;
        transport.notify('competition.steer_consumed', { ...receipt });
      }
    }
  });
  transport.onRequest(async (method, params) => {
    if (method === 'competition/capabilities') return { nativeSteer: true, nativeResume: true, delivery: 'next-step' };
    if (method === 'competition/steer') {
      if (!params || typeof params.requestId !== 'string' || !params.requestId ||
          typeof params.sessionId !== 'string' || !Number.isInteger(params.expectedTurn) ||
          typeof params.text !== 'string' || !params.text.trim()) throw new Error('Invalid steering request');
      const key = `${params.sessionId}:${params.requestId}`;
      const signature = JSON.stringify([params.expectedTurn, params.text]);
      if (receipts.has(key)) {
        if (requests.get(key) !== signature) throw new Error('Steering requestId reused with different content');
        return receipts.get(key);
      }
      const agent = ctx.agents.get(params.sessionId);
      if (!owned.has(params.sessionId) || !agent || agent.status !== 'running' ||
          turns.get(params.sessionId) !== params.expectedTurn) throw new Error('Target turn is not active; steering was NOT queued');
      const message = createUserMessage({ content: [{ type: 'text', text: params.text }], source: { kind: 'user' } });
      agent.steer(message);
      const receipt = { requestId: params.requestId, sessionId: params.sessionId,
        messageId: message.id, turn: params.expectedTurn, accepted: true, consumed: false, delivery: 'next-step' };
      requests.set(key, signature); receipts.set(key, receipt); messages.set(`${params.sessionId}:${message.id}`, receipt);
      return receipt;
    }
    if (method === 'competition/resume') {
      if (!params || typeof params.resumeSessionId !== 'string' || !params.resumeSessionId.trim()) {
        throw new Error('Invalid resume request: resumeSessionId is required');
      }
      const sessionId = params.resumeSessionId;
      if (owned.has(sessionId) || ctx.agents.get(sessionId)) {
        throw new Error(`Native agent "${sessionId}" is already live; refusing to resume over a live session`);
      }
      for (const field of ATTACH) {
        if (server[field] === undefined) {
          throw new Error(`Installed SDK server cannot attach a resumed agent (missing ${field}); refusing to create a fresh session`);
        }
      }
      if (!ctx.get('sessionPersistence')) {
        throw new Error('Session persistence is not configured; refusing to create a fresh session instead of resuming');
      }
      let handle;
      try {
        handle = await ctx.agents.resume({
          resumeSessionId: sessionId,
          agentOptions: {
            provider: server.provider,
            model: server.model,
            ...server.reasoningEffort === undefined ? {} : { reasoningEffort: server.reasoningEffort },
            ...server.maxTokens === undefined ? {} : { maxTokens: server.maxTokens }
          }
        });
      } catch (error) {
        throw new Error(`Native resume failed for "${sessionId}": ${error instanceof Error ? error.message : String(error)}`);
      }
      const agent = handle?.agent;
      if (!agent || String(agent.id) !== sessionId || String(agent.session?.id) !== sessionId) {
        if (handle) await Promise.resolve(handle.dispose()).catch(() => {});
        throw new Error(`Resumed agent identity mismatch for "${sessionId}"; refusing to prompt`);
      }
      // The resumed agent must carry the same effective route as the original
      // SDK handshake. Only fields the runtime actually resolved are compared,
      // so an omitted (adapter-defaulted) effort is not treated as a change.
      const actual = agent.options ?? {};
      const requested = { provider: server.provider, model: server.model,
        ...server.reasoningEffort === undefined ? {} : { reasoningEffort: server.reasoningEffort } };
      const changed = Object.entries(requested)
        .filter(([field, value]) => actual[field] !== undefined && actual[field] !== value);
      if (changed.length > 0) {
        await Promise.resolve(handle.dispose()).catch(() => {});
        throw new Error(`Resumed agent ${changed.map(([field]) => field).join('/')} changed for "${sessionId}"; refusing to prompt`);
      }
      if (actual.provider === undefined || actual.model === undefined) {
        await Promise.resolve(handle.dispose()).catch(() => {});
        throw new Error(`Resumed agent has no resolved provider/model for "${sessionId}"; refusing to prompt`);
      }
      const identity = resumedCwd(agent);
      if (identity !== undefined && identity !== server.cwd) {
        await Promise.resolve(handle.dispose()).catch(() => {});
        throw new Error(`Resumed session "${sessionId}" belongs to another cwd; refusing to prompt`);
      }
      server.sessions.set(sessionId, { handle });
      // Readiness marker only: no session/prompt is sent by this method, so the
      // next prompt from the client is the first new native turn. Steering is
      // accepted from here because the resumed agent is owned by this process.
      owned.add(sessionId);
      return { resumed: true, sessionId: sessionId, agentId: String(agent.id), status: agent.status,
        provider: actual.provider, model: actual.model,
        ...actual.reasoningEffort === undefined ? {} : { reasoningEffort: actual.reasoningEffort },
        deliveredBy: 'ctx.agents.resume', created: false };
    }
    if (method === 'initialize') await ctx.get('loader')?.await();
    const result = await server.handleRequest(method, params);
    if (method === 'session/prompt') owned.add(params.sessionId);
    if (method === 'shutdown') setImmediate(async () => {
      await transport.flush(); await ctx.root.fiber.dispose(); process.exit(0);
    });
    return result;
  });
  ctx.effect(() => {
    transport.start();
    return async () => { off(); await server.shutdown(); transport.close(); };
  }, 'competition.sdk-steering');
}
