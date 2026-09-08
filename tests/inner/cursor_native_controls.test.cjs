"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { createControls, observeReact } = require("../../omnigent/inner/cursor_protocol/controls.cjs");

test("live Run Everything changes are observed and checked again before automatic replies", async () => {
  const events = [];
  const controls = createControls(event => events.push(event));
  let mode = "unrestricted";
  let approved = 0;
  const agentStore = { getMetadata: key => key === "approvalMode" ? mode : true };
  const props = { value: { agentStore, isRunEverything: true } };
  controls.attach(props);
  assert.equal((await controls.dispatch({ action: "get_approval_config" })).approval_mode, "unrestricted");
  controls.attach({ pendingDecisionStore: {
    subscribe: () => () => {}, findDecision: () => ({}), approveDecision: () => approved++,
  } });
  await controls.dispatch({ action: "reply", id: "tool", outcome: "accept", require_run_everything: true });
  assert.equal(approved, 1);
  mode = "allowlist";
  assert.deepEqual(await controls.dispatch({ action: "get_approval_config" }),
    { approval_mode: "allowlist", run_everything: false, source: "cursor-runtime" });
  await assert.rejects(controls.dispatch({ action: "reply", id: "tool", outcome: "accept",
    require_run_everything: true }), /no longer enabled/);
  controls.attach(props);
  assert.deepEqual(events.filter(e => e.kind === "approval_mode").map(e => e.message.runEverything), [true, false]);
  assert.equal(approved, 1);
  controls.close();
});

test("native submit works before any agent run and keeps React output unchanged", async () => {
  const seen = [];
  const controls = createControls(() => {});
  const react = { jsx: (_type, props) => props, jsxs: (_type, props) => props };
  observeReact(react, controls);
  const props = { getChatId: () => "chat", sendMessage: (text) => seen.push(text), onAbort() {} };
  assert.equal(react.jsx("Chat", props), props);
  await controls.dispatch({ action: "prompt", content: "first\nmessage" });
  assert.deepEqual(seen, ["first\nmessage"]);
});

test("tool replies resolve the native store and reject stale decision ids", async () => {
  const controls = createControls(() => {});
  const decisions = [{ id: "decision", operation: { type: "shell", toolCallId: "call" } }];
  const store = {
    subscribe(fn) { fn(decisions); return () => {}; },
    findDecision(id) { return decisions.find((d) => d.id === id); },
    approveDecision(id) { decisions.splice(decisions.findIndex((d) => d.id === id), 1); },
    rejectDecision() { throw new Error("not expected"); },
  };
  controls.attach({ pendingDecisionStore: store });
  await controls.dispatch({ action: "reply", id: "decision", outcome: "accept" });
  assert.equal(decisions.length, 0);
  await assert.rejects(controls.dispatch({ action: "reply", id: "decision", outcome: "accept" }), /no longer pending/);
});

test("compact and stop call native callbacks, and blocked prompts fail explicitly", async () => {
  const seen = [];
  const controls = createControls(() => {});
  controls.attach({ getChatId: () => "chat", sendMessage() {}, onAbort: () => seen.push("cancel"),
    onSummarize: () => seen.push("compact"), shouldBlockSubmit: true });
  await assert.rejects(controls.dispatch({ action: "prompt", content: "hello" }), /not accepting/);
  await controls.dispatch({ action: "cancel" });
  await controls.dispatch({ action: "compact" });
  assert.deepEqual(seen, ["cancel", "compact"]);
});

test("question answers update native selection before submitting, preserving option ids and free text", async () => {
  const events = [];
  const controls = createControls((event) => events.push(event));
  const args = { questions: [{ id: "q", prompt: "Pick", options: [{ id: "a", label: "Alpha" }] }] };
  let submitted;
  const props = { getChatId: () => "chat", sendMessage() {}, onAbort() {},
    askQuestionPrompt: { args, selection: {} },
    onAskQuestionSelectionChange(selection) {
      setImmediate(() => {
        props.askQuestionPrompt = { args, selection };
        controls.attach(props);
      });
    },
    onAskQuestionSubmit() { submitted = props.askQuestionPrompt.selection; },
  };
  controls.attach(props);
  const id = events.find((e) => e.kind === "decision_pending").message.id;
  await controls.dispatch({ action: "reply", id, outcome: "accept", answers: { q: ["Alpha", "my answer"] } });
  assert.deepEqual(submitted.selectionsByQuestionId.q, ["a", "__freeform_other__"]);
  assert.equal(submitted.freeformTextsByQuestionId.q, "my answer");
  controls.attach({ ...props, askQuestionPrompt: null });
  await assert.rejects(controls.dispatch({ action: "reply", id, outcome: "accept" }), /no longer pending/);
});

test("a prompt acknowledgement does not wait for the model to finish", async () => {
  const controls = createControls(() => {});
  controls.attach({ getChatId: () => "chat", sendMessage: () => new Promise(() => {}), onAbort() {} });
  assert.deepEqual(await controls.dispatch({ action: "prompt", content: "continue" }), { status: "submitted" });
});

test("model changes await the native selection result and preserve errors", async () => {
  const controls = createControls(() => {});
  const calls = [];
  controls.attach({ getChatId: () => "chat", sendMessage() {}, onAbort() {},
    async onSelectModelWithParams(id, params) { calls.push([id, params]); throw new Error("unavailable model"); },
  });
  await assert.rejects(controls.dispatch({ action: "model", model: "selected" }), /unavailable model/);
  assert.deepEqual(calls, [["selected", {}]]);
});

test("asynchronous command failures remain observable after submission is acknowledged", async () => {
  const events = [];
  let fail;
  const controls = createControls(event => events.push(event));
  controls.attach({ getChatId: () => "chat", onAbort() {},
    sendMessage: () => new Promise((_resolve, reject) => { fail = reject; }) });
  assert.deepEqual(await controls.dispatch({ action: "prompt", content: "hello" }), { status: "submitted" });
  fail(new Error("Submission failed"));
  await new Promise(setImmediate);
  const error = events.find(event => event.kind === "command_error");
  assert.equal(error.message.message, "Submission failed");
  assert.equal(error.conversationId, "chat");
  assert.equal(error.root, true);
});
