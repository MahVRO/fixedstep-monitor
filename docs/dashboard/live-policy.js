export function shouldAdoptLatest({
  source,
  hasLatest,
  hasRelay,
  currentSource,
  incomingRelayReceived,
  currentRelayReceived,
  incomingGenerated,
  currentGenerated,
  incomingCollected,
  currentCollected,
}) {
  if (source === "live") {
    if (!hasRelay) return false;
    if (!hasLatest) return true;
    if (currentSource !== "live") {
      return Number.isFinite(incomingCollected)
        && (!Number.isFinite(currentCollected) || incomingCollected >= currentCollected);
    }
    return incomingRelayReceived > currentRelayReceived
      || (incomingRelayReceived === currentRelayReceived && incomingGenerated >= currentGenerated);
  }
  if (!hasLatest) return true;
  if (source === "history") return false;
  return incomingCollected > currentCollected
    || (incomingCollected === currentCollected && incomingGenerated > currentGenerated);
}

export function isHealthyLive({
  fresh,
  status,
  connected,
  latestSource,
  liveTransportOk,
  hasRelay,
}) {
  return fresh
    && status === "live"
    && connected === true
    && latestSource === "live"
    && liveTransportOk === true
    && hasRelay;
}
