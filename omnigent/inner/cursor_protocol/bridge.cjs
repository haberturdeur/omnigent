"use strict";

const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const { randomUUID } = require("node:crypto");
const { createControls, observeReact } = require("./controls.cjs");

const SUPPORTED_VERSION = "2026.09.02-c22c1a3";
const ENTRY = 'var __webpack_exports__=__webpack_require__("./src/main.tsx")';

function instrumentClient(client, emit, active, decodeCheckpoint) {
  const originalRun = client.run;
  let failedToPublish = false;
  if (typeof originalRun !== "function") throw new Error("Unsupported Cursor agent client");
  client.run = async function (...args) {
    const listener = args[4];
    const actions = args[7];
    const store = args[8];
    const conversationId = args[10]?.conversationId;
    if (!conversationId || typeof listener?.sendUpdate !== "function" ||
        typeof actions?.submitConversationAction !== "function" ||
        typeof store?.handleCheckpoint !== "function") {
      throw new Error("Unsupported Cursor native run signature");
    }
    const root = !args[10]?.parentAgentToolCallId && !args[10]?.parentRequestId;
    const publish = (kind, message) => {
      try { emit({ conversationId, root, kind, message }); }
      catch (error) {
        if (!failedToPublish) process.stderr.write(`Cursor native observation failed: ${error.message}\n`);
        failedToPublish = true;
      }
    };
    const snapshot = async (ctx, state) => {
      // Checkpoints contain blob ids. Resolve todos without decoding the entire transcript.
      const view = state.clone();
      view.turns = [];
      view.summary = undefined;
      const conversation = decodeCheckpoint
        ? await decodeCheckpoint(ctx, view, args[6])
        : await store.deserializeConversationStateStructure(ctx, view);
      if (conversation.todos.length !== view.todos.length) return undefined;
      return { todos: conversation.todos.map((todo) => todo.toJson()),
        subagents: Object.entries(state.subagentStates || {}).map(([id, child]) => ({
          id, title: `Cursor ${Object.keys(child.subagentType?.toJson() || {})[0] || "subagent"}`,
        })),
        tokenDetails: state.tokenDetails?.toJson() };
    };
    const observeCheckpoint = async (ctx, state) => {
      try {
        const decoded = await snapshot(ctx, state);
        if (decoded) publish("conversation_checkpoint_update", decoded);
      } catch (error) {
        if (!failedToPublish) process.stderr.write(`Cursor checkpoint observation failed: ${error.message}\n`);
        failedToPublish = true;
      }
    };
    args[4] = new Proxy(listener, {
      get(target, key) {
        if (key === "sendUpdate") return async (ctx, update) => {
          publish("interaction_update", update.toJson());
          return target.sendUpdate(ctx, update);
        };
        const value = Reflect.get(target, key);
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    args[8] = new Proxy(store, {
      get(target, key) {
        if (key === "handleCheckpoint") return async (ctx, state, metadata) => {
          await target.handleCheckpoint(ctx, state, metadata);
          await observeCheckpoint(ctx, state);
        };
        const value = Reflect.get(target, key);
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    active.set(conversationId, actions);
    try {
      publish("run_started", {});
      await observeCheckpoint(args[0], args[1]);
      return await originalRun.apply(this, args);
    } catch (error) {
      if (!args[0]?.canceled) publish("error", { message: error.message });
      throw error;
    } finally {
      if (active.get(conversationId) === actions) active.delete(conversationId);
      publish("run_ended", {});
    }
  };
}

async function loadRuntime(entryPath) {
  const entry = fs.realpathSync(entryPath);
  if (path.basename(path.dirname(entry)) !== SUPPORTED_VERSION) {
    throw new Error(`Cursor protocol bridge requires CLI ${SUPPORTED_VERSION}`);
  }
  const source = fs.readFileSync(entry, "utf8");
  if (source.split(ENTRY).length !== 2) throw new Error("Unsupported Cursor bundle entry");
  const vendor = new Module(entry, module);
  vendor.filename = entry;
  vendor.paths = Module._nodeModulePaths(path.dirname(entry));
  vendor._compile(source.replace(ENTRY, "module.exports=__webpack_require__"), entry);
  const runtime = vendor.exports;
  await runtime.e(7569);
  return runtime;
}

async function main() {
  const entry = process.argv[2];
  const runtime = await loadRuntime(entry);
  const proto = runtime("../proto/dist/generated/agent/v1/agent_pb.js");
  if (process.argv.includes("--probe")) {
    const { pH: Store } = runtime("../agent-kv/dist/index.js");
    const { kW: Todo } = runtime("../proto/dist/generated/agent/v1/todo_tool_pb.js");
    const todo = new Todo({ id: "probe", content: "Protocol probe", status: 1 });
    const store = new Store({ getBlob: async () => todo.toBinary(), setBlob: async () => {} },
      { set() {} });
    const state = new proto.ConversationStateStructure({ todos: [new Uint8Array([1])] });
    const events = [];
    const client = { run: async (...args) => args[8].handleCheckpoint({}, state) };
    instrumentClient(client, (event) => events.push(event), new Map());
    await client.run({}, state, {}, {}, { sendUpdate() {} }, {}, {},
      { submitConversationAction() {} }, store, [], { conversationId: "probe" });
    if (events.findLast((e) => e.kind === "conversation_checkpoint_update")?.message.todos[0]?.id !== "probe") {
      throw new Error("Native checkpoint decoding probe failed");
    }
    process.stdout.write(JSON.stringify({ version: SUPPORTED_VERSION, nativeProtocol: "agent.v1", ready: true }) + "\n");
    return;
  }
  const directory = process.argv[3];
  if (!directory || process.argv[4] !== "--") throw new Error("Expected bridge directory and -- before Cursor arguments");
  const active = new Map();
  const channel = createSideChannel(directory, active, proto);
  process.once("exit", () => channel.close());
  await startTerminal(runtime, entry, process.argv.slice(5), channel.emit, active, channel.controls);
}

async function startTerminal(runtime, entry, args, emit, active, controls) {
  if (controls) {
    const key = Object.keys(runtime.m).find((key) => key.endsWith("/react/jsx-runtime.js"));
    if (!key) throw new Error("Cursor React runtime is missing");
    observeReact(runtime(key), controls);
  }
  const { PW: Client } = runtime("../agent-client/dist/index.js");
  const decode = async (ctx, state, blobs) => {
    const { pH: Store } = runtime("../agent-kv/dist/index.js");
    return new Store(blobs, {}).deserializeConversationStateStructure(ctx, state);
  };
  instrumentClient(Client.prototype, emit, active, decode);
  process.argv = [process.execPath, entry, ...args];
  await runtime("./src/main.tsx");
}

function createSideChannel(directory, active, proto) {
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  fs.chmodSync(directory, 0o700);
  const epoch = randomUUID();
  const eventsPath = path.join(directory, `events.${epoch}.jsonl`);
  const descriptor = fs.openSync(eventsPath, "ax", 0o600);
  const atomic = (name, value) => {
    const destination = path.join(directory, name);
    const temporary = `${destination}.${epoch}.tmp`;
    fs.writeFileSync(temporary, JSON.stringify(value), { mode: 0o600 });
    fs.renameSync(temporary, destination);
  };
  let sequence = 0;
  const pending = new Map();
  const state = { epoch, pid: process.pid, conversationId: null, running: false, controlsReady: false, pending: [] };
  atomic("active.json", state);
  const emit = (event) => {
    if (!event.root) return;
    if (event.kind === "approval_mode") {
      state.runEverything = event.message.runEverything;
      atomic("active.json", state);
    }
    if (event.kind === "session_ready") {
      state.controlsReady = true;
      state.conversationId = event.conversationId;
      atomic("active.json", state);
    }
    if (event.kind === "decision_pending" || event.kind === "decision_resolved") {
      if (event.kind === "decision_pending") pending.set(event.message.id, event.message);
      else pending.delete(event.message.id);
      state.pending = [...pending.values()];
      atomic("active.json", state);
    }
    if (event.kind === "run_started" || event.kind === "run_ended") {
      state.conversationId = event.conversationId;
      state.running = event.kind === "run_started";
      atomic("active.json", state);
    }
    if (event.kind === "interaction_update" && ![
      "summaryStarted", "summaryCompleted", "summary",
    ].some((key) => key in event.message)) return;
    fs.writeSync(descriptor, JSON.stringify({ ...event, epoch, sequence: ++sequence }) + "\n");
  };
  const controls = createControls(emit);
  const inflight = new Set();
  const poll = () => {
    for (const name of fs.readdirSync(directory)) {
      if (!/^request\.[a-f0-9]{32}\.json$/.test(name) || inflight.has(name)) continue;
      inflight.add(name);
      const filename = path.join(directory, name);
      const claimed = path.join(directory, name.replace("request.", "processing."));
      try { fs.renameSync(filename, claimed); }
      catch { inflight.delete(name); continue; }
      Promise.resolve().then(async () => {
        const request = JSON.parse(fs.readFileSync(claimed, "utf8"));
        if (request.epoch !== epoch) throw new Error("Cursor terminal was restarted");
        if (request.deadline < Date.now()) throw new Error("Cursor control request expired");
        return await controls.dispatch(request);
      }).then((result) => ({ ok: true, ...result }), (error) => ({ ok: false, error: error.message }))
        .then((result) => {
          atomic(name.replace("request.", "response."), result);
          fs.rmSync(claimed, { force: true });
        }).catch((error) => process.stderr.write(`Cursor protocol channel: ${error.message}\n`))
        .finally(() => inflight.delete(name));
    }
  };
  const timer = setInterval(() => {
    try { poll(); } catch (error) { process.stderr.write(`Cursor protocol channel: ${error.message}\n`); }
  }, 100);
  timer.unref();
  return { emit, poll, epoch, controls, close() {
    controls.close();
    clearInterval(timer);
    state.running = false;
    state.controlsReady = false;
    state.pending = [];
    atomic("active.json", state);
    fs.closeSync(descriptor);
  } };
}

module.exports = { instrumentClient, loadRuntime, startTerminal, createSideChannel };
if (require.main === module) main().catch((error) => {
  process.stderr.write(`Cursor native bridge: ${error.message}\n`);
  process.exitCode = 1;
});
