"use strict";

const { randomUUID } = require("node:crypto");

function observeReact(react, controls) {
  for (const key of ["jsx", "jsxs"]) {
    const original = react[key];
    if (typeof original !== "function") throw new Error("Unsupported Cursor React runtime");
    react[key] = function (type, props, ...rest) {
      controls.attach(props);
      return original.call(this, type, props, ...rest);
    };
  }
}

function createControls(emit) {
  let current;
  let store;
  let unsubscribe;
  let agentStore;
  let reportedRunEverything;
  const approvalConfig = () => {
    if (!agentStore) throw new Error("Cursor approval configuration is not ready");
    const mode = agentStore.getMetadata("approvalMode") ??
      (agentStore.getMetadata("isRunEverything") === true ? "unrestricted" : "allowlist");
    if (!["allowlist", "unrestricted", "auto-review"].includes(mode)) {
      throw new Error("Unsupported Cursor approval mode");
    }
    return { approval_mode: mode, run_everything: mode === "unrestricted", source: "cursor-runtime" };
  };
  const runEverything = () => {
    try { return approvalConfig().run_everything; } catch { return undefined; }
  };
  let published = new Map();
  const slots = new Map();
  const waiters = new Set();
  let failedToPublish = false;
  const publish = (kind, message) => {
    try { emit({ root: true, kind, message, conversationId: current?.getChatId() }); }
    catch (error) {
      if (!failedToPublish) process.stderr.write(`Cursor native control observation failed: ${error.message}\n`);
      failedToPublish = true;
    }
  };
  const start = (fn) => {
    // Native submit promises cover the model run; acknowledge dispatch without waiting for it.
    Promise.resolve(fn()).catch((error) => publish("command_error", { message: error.message }));
    return { status: "submitted" };
  };
  const syncSlot = (kind, value, args) => {
    const old = slots.get(kind);
    if (old?.value === value) return;
    if (old) publish("decision_resolved", { id: old.id });
    slots.delete(kind);
    if (value) {
      const slot = { id: randomUUID(), value, kind, args };
      slots.set(kind, slot);
      publish("decision_pending", { id: slot.id, kind, args: args?.toJson?.() ?? args });
    }
  };
  const attach = (props) => {
    if (!props) return;
    if (props.value?.agentStore && typeof props.value.isRunEverything === "boolean") {
      agentStore = props.value.agentStore;
      const enabled = runEverything();
      if (enabled !== reportedRunEverything) {
        reportedRunEverything = enabled;
        publish("approval_mode", { runEverything: enabled });
      }
    }
    if (props.pendingDecisionStore && props.pendingDecisionStore !== store) {
      unsubscribe?.();
      store = props.pendingDecisionStore;
      unsubscribe = store.subscribe((decisions) => {
        const next = new Map(decisions.map((d) => [d.id, d]));
        for (const id of published.keys()) if (!next.has(id)) publish("decision_resolved", { id });
        for (const [id, decision] of next) if (!published.has(id)) {
          publish("decision_pending", { id, kind: "tool", args: decision.operation });
        }
        published = next;
      });
    }
    if (typeof props.sendMessage !== "function" || typeof props.getChatId !== "function" ||
        typeof props.onAbort !== "function") return;
    const changed = current?.getChatId() !== props.getChatId();
    current = props;
    if (changed) publish("session_ready", {});
    syncSlot("question", props.askQuestionPrompt?.args, props.askQuestionPrompt?.args);
    syncSlot("mode", props.switchModePrompt?.args, props.switchModePrompt?.args);
    syncSlot("web", props.sessionDecision?.id, props.sessionDecision?.operation);
    for (const notify of waiters) notify();
  };
  const waitForSelection = (id, selection) => new Promise((resolve, reject) => {
    const notify = () => {
      if (slots.get("question")?.id !== id) return done(new Error("Question is no longer pending"));
      const actual = current.askQuestionPrompt?.selection;
      if (JSON.stringify(actual?.selectionsByQuestionId) === JSON.stringify(selection.selectionsByQuestionId) &&
          JSON.stringify(actual?.freeformTextsByQuestionId) === JSON.stringify(selection.freeformTextsByQuestionId)) done();
    };
    const done = (error) => { clearTimeout(timer); waiters.delete(notify); error ? reject(error) : resolve(); };
    const timer = setTimeout(() => done(new Error("Cursor did not apply the question selection")), 5000);
    waiters.add(notify);
    notify();
  });
  const dispatch = async (request) => {
    if (request.action === "get_approval_config") return approvalConfig();
    if (request.action === "reply") {
      if (request.require_run_everything === true && runEverything() !== true) {
        throw new Error("Cursor Run Everything is no longer enabled");
      }
      if (!["accept", "decline", "cancel"].includes(request.outcome)) throw new Error("Invalid decision outcome");
      if (store?.findDecision(request.id)) {
        if (request.outcome === "accept") store.approveDecision(request.id);
        else store.rejectDecision(request.id, request.feedback || "Rejected in Omnigent");
        return {};
      }
      const slot = [...slots.values()].find((slot) => slot.id === request.id);
      if (!slot) throw new Error("Decision is no longer pending");
      if (slot.kind === "question") {
        if (request.outcome !== "accept") { current.onAskQuestionSkip(); return {}; }
        const selection = { activeQuestionIndex: 0, activeOptionIndex: 0,
          selectionsByQuestionId: {}, freeformTextsByQuestionId: {} };
        for (const question of slot.args.questions) {
          const answer = request.answers?.[question.id];
          const labels = Array.isArray(answer) ? answer : [answer];
          if (!labels.length || labels.some((label) => typeof label !== "string" || !label.trim())) {
            throw new Error("Answer every question before submitting");
          }
          const ids = [];
          const free = [];
          for (const label of labels) {
            const options = question.options.filter((option) => option.label === label);
            if (options.length > 1) throw new Error("Ambiguous option label");
            if (options.length) ids.push(options[0].id); else free.push(label);
          }
          selection.selectionsByQuestionId[question.id] = [...ids, ...(free.length ? ["__freeform_other__"] : [])];
          selection.freeformTextsByQuestionId[question.id] = free.join("\n");
        }
        current.onAskQuestionSelectionChange(selection);
        await waitForSelection(slot.id, selection);
        current.onAskQuestionSubmit();
      } else if (slot.kind === "mode") {
        if (request.outcome === "accept") current.onSwitchModeApprove(); else current.onSwitchModeReject();
      } else {
        if (request.outcome === "accept") current.onSessionDecisionApprove();
        else current.onSessionDecisionReject(request.feedback || "Rejected in Omnigent");
      }
      return {};
    }
    if (!current) throw new Error("Cursor native controls are not ready");
    if (request.action === "prompt") {
      if (typeof request.content !== "string" || !request.content.trim()) throw new Error("Empty Cursor prompt");
      if (current.shouldBlockSubmit || current.shouldDisableInput || current.isResumingChat) {
        throw new Error("Cursor is not accepting prompts while a dialog or session transition is active");
      }
      return start(() => current.sendMessage(request.content));
    }
    if (request.action === "cancel") { current.onAbort(); return {}; }
    if (request.action === "compact") {
      if (current.isGenerating) throw new Error("Wait for the current Cursor turn before compacting");
      return start(() => current.onSummarize());
    }
    if (request.action === "model") {
      if (typeof request.model !== "string" || !request.model.trim()) throw new Error("Empty Cursor model");
      await current.onSelectModelWithParams(request.model, {});
      return {};
    }
    throw new Error("Unsupported native control");
  };
  return { attach, dispatch, close: () => unsubscribe?.() };
}

module.exports = { observeReact, createControls };
