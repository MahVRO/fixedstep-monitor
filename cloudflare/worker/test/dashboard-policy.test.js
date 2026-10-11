import assert from "node:assert/strict";
import test from "node:test";

import {
  isHealthyLive,
  shouldAdoptLatest,
} from "../../../docs/dashboard/live-policy.js";

function candidate(overrides = {}) {
  return {
    source: "live",
    hasLatest: false,
    hasRelay: true,
    currentSource: null,
    incomingRelayReceived: 20,
    currentRelayReceived: -Infinity,
    incomingGenerated: 20,
    currentGenerated: -Infinity,
    incomingCollected: 20,
    currentCollected: -Infinity,
    ...overrides,
  };
}

test("a cold-start live payload is accepted only with validated relay metadata", () => {
  assert.equal(shouldAdoptLatest(candidate()), true);
  assert.equal(shouldAdoptLatest(candidate({ hasRelay: false })), false);
  assert.equal(shouldAdoptLatest(candidate({ source: "static", hasRelay: false })), true);
});

test("live ordering uses relay receipt time and history cannot replace latest", () => {
  assert.equal(shouldAdoptLatest(candidate({
    hasLatest: true,
    currentSource: "live",
    currentRelayReceived: 21,
  })), false);
  assert.equal(shouldAdoptLatest(candidate({
    hasLatest: true,
    currentSource: "live",
    currentRelayReceived: 19,
  })), true);
  assert.equal(shouldAdoptLatest(candidate({
    source: "history",
    hasLatest: true,
  })), false);
  assert.equal(shouldAdoptLatest(candidate({
    hasLatest: true,
    currentSource: "static",
    incomingCollected: 19,
    currentCollected: 20,
  })), false);
  assert.equal(shouldAdoptLatest(candidate({
    hasLatest: true,
    currentSource: "static",
    incomingCollected: 21,
    currentCollected: 20,
  })), true);
});

test("healthy live state always requires relay freshness evidence", () => {
  const healthy = {
    fresh: true,
    status: "live",
    connected: true,
    latestSource: "live",
    liveTransportOk: true,
    hasRelay: true,
  };
  assert.equal(isHealthyLive(healthy), true);
  assert.equal(isHealthyLive({ ...healthy, hasRelay: false }), false);
  assert.equal(isHealthyLive({ ...healthy, liveTransportOk: false }), false);
  assert.equal(isHealthyLive({ ...healthy, fresh: false }), false);
  assert.equal(isHealthyLive({ ...healthy, connected: null }), false);
});
