"use strict";

// Exact public origin of the quote-only relay. No credential is used by readers.
// Keeping this separate from app.js lets the static dashboard remain unchanged.
window.FIXEDSTEP_MONITOR_CONFIG = Object.freeze({
  liveOrigin: "https://fixedstep-quote-relay.fixedstep-quote-relay.workers.dev",
});
