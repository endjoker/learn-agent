import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";

const source = fs.readFileSync("gateway/webui/static/pages/chat.js", "utf8");
const context = {
  window: {},
  HA: { renderMd: text => `<p>${text}</p>` },
  document: {},
  setTimeout,
  clearTimeout,
};
vm.runInNewContext(source, context, { filename: "chat.js" });
const PageChat = context.window.PageChat;

function makePage() {
  const added = [];
  return Object.assign(Object.create(PageChat.prototype), {
    _streamBubble: null,
    _streamText: "",
    _closedStreamIds: new Set(),
    _msgArea: { appendChild: bubble => added.push(bubble) },
    _bubble() {
      const body = { innerHTML: "" };
      return {
        removed: false,
        querySelector: selector => selector === ".bubble-text" ? body : null,
        remove() { this.removed = true; },
        body,
      };
    },
    _scrollBottom() {},
    _appendAssistant(text) { added.push({ assistant: text }); },
    added,
  });
}

test("streaming bubble accumulates deltas and ignores a late delta after completion", () => {
  const page = makePage();
  PageChat.prototype._appendStreamDelta.call(page, "Hel", "message-1");
  PageChat.prototype._appendStreamDelta.call(page, "lo", "message-1");

  assert.equal(page.added.length, 1);
  assert.equal(page._streamBubble.body.innerHTML, "<p>Hello</p>");

  PageChat.prototype._finishStream.call(page, "Hello", "message-1");
  PageChat.prototype._appendStreamDelta.call(page, " late", "message-1");

  assert.equal(page._streamBubble, null);
  assert.equal(page.added.length, 1);
  assert.equal(page.added[0].body.innerHTML, "<p>Hello</p>");
});

test("a new stream reset removes only the active bubble", () => {
  const page = makePage();
  PageChat.prototype._appendStreamDelta.call(page, "partial", "message-2");
  const bubble = page._streamBubble;

  PageChat.prototype._resetStream.call(page, "message-2");

  assert.equal(bubble.removed, true);
  assert.equal(page._streamBubble, null);
  assert.equal(page._streamText, "");
});
