"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { instrumentClient, loadRuntime, startTerminal, createSideChannel } = require("../../omnigent/inner/cursor_protocol/bridge.cjs");

const proto = (value) => ({ ...value, toJson: () => value, clone: () => proto(value) });

test("native checkpoints expose stable child ids and agent types", async () => {
  const f = fixture(async () => {});
  f.args[1] = proto({ todos: [], subagentStates: {
    "native-child": { subagentType: proto({ shell: {} }) },
  } });
  await f.client.run(...f.args);
  assert.deepEqual(f.events.find(e => e.kind === "conversation_checkpoint_update").message.subagents,
    [{ id: "native-child", title: "Cursor shell" }]);
});

function fixture(run) {
  const events = [];
  const active = new Map();
  const saved = [];
  const delivered = [];
  const store = {
    async handleCheckpoint(...args) { saved.push(args); },
    async deserializeConversationStateStructure(_ctx, state) {
      assert.deepEqual(state.turns, []);
      assert.equal(state.summary, undefined);
      return { todos: state.todos.map(proto) };
    },
  };
  const listener = {
    async sendUpdate(...args) { delivered.push(args); },
    async query(_ctx, query) { return { query, approved: false }; },
  };
  const actions = { submitConversationAction() {} };
  const args = [{}, proto({ todos: [] }), {}, {}, listener, {}, {}, actions, store, [], { conversationId: "native-id" }];
  const client = { run };
  instrumentClient(client, (event) => events.push(event), active);
  return { client, args, events, active, saved, delivered };
}

test("native updates preserve query ownership and persist checkpoints before publishing", async () => {
  const update = proto({ summaryStarted: {} });
  const checkpoint = proto({ todos: [{ id: "one", content: "Work", status: "pending" }] });
  const f = fixture(async (...args) => {
    assert.equal(f.active.get("native-id"), args[7]);
    await args[4].sendUpdate(args[0], update);
    assert.deepEqual(await args[4].query(args[0], "approval"), { query: "approval", approved: false });
    await args[8].handleCheckpoint(args[0], checkpoint, "metadata");
    assert.equal(f.saved.length, 1);
    return "finished";
  });
  assert.equal(await f.client.run(...f.args), "finished");
  assert.equal(f.active.size, 0);
  assert.equal(f.delivered[0][1], update);
  assert.equal(f.saved[0][1], checkpoint);
  assert.deepEqual(f.events.findLast((e) => e.kind === "conversation_checkpoint_update").message.todos, checkpoint.toJson().todos);
  assert.equal(f.events.at(-1).root, true);
});

test("failed checkpoint persistence never announces a saved checkpoint", async () => {
  const f = fixture(async (...args) => args[8].handleCheckpoint({}, proto({ todos: [] })));
  f.args[8].handleCheckpoint = async () => { throw new Error("disk full"); };
  await assert.rejects(f.client.run(...f.args), /disk full/);
  assert.equal(f.events.filter((e) => e.kind === "conversation_checkpoint_update").length, 1);
  assert.equal(f.events.at(-2).kind, "error");
  assert.equal(f.active.size, 0);
});

test("unexpected native run signatures fail before tool execution", async () => {
  let ran = false;
  const f = fixture(async () => { ran = true; });
  f.args[7] = {};
  await assert.rejects(f.client.run(...f.args), /Unsupported Cursor native run signature/);
  assert.equal(ran, false);
});

test("missing KV blobs never publish a partial authoritative todo list", async () => {
  const f = fixture(async (...args) => args[8].handleCheckpoint({}, proto({ todos: ["blob-id"] })));
  f.args[8].deserializeConversationStateStructure = async () => ({ todos: [] });
  await f.client.run(...f.args);
  assert.equal(f.events.filter((event) => event.kind === "conversation_checkpoint_update").length, 1);
  assert.equal(f.saved.length, 1);
});

test("unsupported installed bundle fails closed", async () => {
  await assert.rejects(loadRuntime(__filename), /Unsupported Cursor bundle entry/);
});

test("a bundle whose exports were renamed fails closed", () => {
  const { nativeExports } = require("../../omnigent/inner/cursor_protocol/bridge.cjs");
  const runtime = (name) => (name === "../agent-client/dist/index.js" ? { PW: class {} } : {});
  runtime.m = {};
  assert.throws(() => nativeExports(runtime), /missing the native protocol interfaces/);
});

test("a bundle whose modules moved reports the missing module", () => {
  const { nativeExports } = require("../../omnigent/inner/cursor_protocol/bridge.cjs");
  // Webpack throws for an unknown id rather than returning an empty module.
  const runtime = (name) => { throw new Error(`Cannot find module '${name}'`); };
  runtime.m = {};
  assert.throws(() => nativeExports(runtime), /Cannot find module/);
});

test("terminal bootstrap preserves stdio and native arguments without ACP", async () => {
  const input = process.stdin;
  const output = process.stdout;
  const argv = process.argv;
  const called = [];
  class Client { async run() {} }
  const runtime = (name) => {
    called.push(name);
    if (name === "../agent-client/dist/index.js") return { PW: Client };
    if (name === "./src/main.tsx") {
      assert.equal(process.stdin, input);
      assert.equal(process.stdout, output);
      assert.deepEqual(process.argv.slice(2), ["--resume", "chat", "--model", "chosen"]);
    }
  };
  try {
    await startTerminal(runtime, "/cursor/index.js", ["--resume", "chat", "--model", "chosen"], () => {}, new Map());
    assert.deepEqual(called, ["../agent-client/dist/index.js", "./src/main.tsx"]);
  } finally { process.argv = argv; }
});

test("file requests dispatch once and stale terminal generations cannot submit", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cursor-channel-"));
  const channel = createSideChannel(dir, new Map(), {});
  const seen = [];
  channel.controls.attach({ getChatId: () => "chat", sendMessage: (text) => seen.push(text), onAbort() {} });
  try {
    channel.emit({ root: true, kind: "approval_mode", message: { runEverything: true } });
    assert.equal(JSON.parse(fs.readFileSync(path.join(dir, "active.json"))).runEverything, true);
    channel.emit({ root: true, kind: "approval_mode", message: { runEverything: false } });
    assert.equal(JSON.parse(fs.readFileSync(path.join(dir, "active.json"))).runEverything, false);
    for (const [id, epoch] of [["a".repeat(32), channel.epoch], ["b".repeat(32), "stale"]]) {
      fs.writeFileSync(path.join(dir, `request.${id}.json`), JSON.stringify({ epoch,
        deadline: Date.now() + 5000, action: "prompt", content: "hello" }));
      channel.poll();
      channel.poll();
      await new Promise(setImmediate);
      const result = JSON.parse(fs.readFileSync(path.join(dir, `response.${id}.json`)));
      assert.equal(result.ok, epoch === channel.epoch);
    }
    assert.deepEqual(seen, ["hello"]);
  } finally {
    channel.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("checkpoint observation failures do not prevent native execution or persistence", async () => {
  const f = fixture(async (...args) => {
    await args[8].handleCheckpoint({}, proto({ todos: [] }));
    return "finished";
  });
  f.args[8].deserializeConversationStateStructure = async () => { throw new Error("bad observation blob"); };
  assert.equal(await f.client.run(...f.args), "finished");
  assert.equal(f.saved.length, 1);
  assert.equal(f.events.filter(e => e.kind === "conversation_checkpoint_update").length, 0);
});
