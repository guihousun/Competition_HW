// Project-local transport adapter. All agent/context behavior stays native DSH.
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const require = createRequire(process.env.COMPETITION_DSH_PACKAGE_JSON);
const native = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-sdk-jsonrpc-server')).href);
const { JsonRpcLineTransport } = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-sdk-protocol')).href);
const { createUserMessage } = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-llm')).href);
export const name = 'competition-steering-server';
export const inject = ['agents', 'sessions', 'loader'];
export const Config = native.Config;

export function apply(ctx, config) {
  const transport = new JsonRpcLineTransport(process.stdin, process.stdout);
  const server = new native.HarnessSdkJsonRpcServer(ctx, transport, { maxTokensAsSuccess: false });
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
    if (method === 'competition/capabilities') return { nativeSteer: true, delivery: 'next-step' };
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
