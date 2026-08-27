/**
 * Acurast Opportunity Scanner  (Engine 3 — keep phones busy / feed Operator)
 * Runs ON the processor. Fetches sources, scores them with a simple heuristic,
 * and forwards anything above MIN_SCORE to a webhook. Uses only the documented
 * processor runtime API (print, httpGET, _STD_.env).
 */
function score(text) {
  // crude signal scoring: mentions of high-intent keywords raise the score
  var hints = ["earn", "usdc", "acurast", "compute", "depin", "grant", "bounty", "api"];
  var s = 0;
  hints.forEach(function (h) {
    if (text.toLowerCase().indexOf(h) !== -1) s += 0.12;
  });
  return Math.min(1.0, s);
}

function scan(src) {
  print("scanning " + src);
  httpGET(
    src,
    { "Accept": "text/html, application/json, */*" },
    function (payload) {
      var s = score(payload);
      if (s >= parseFloat(_STD_.env["MIN_SCORE"] || "0.3")) {
        var out = JSON.stringify({
          source: src,
          score: s,
          ts: Date.now(),
          processor: _STD_.device.getAddress(),
          snippet: payload.substring(0, 280),
        });
        print("OPPORTUNITY " + src + " score=" + s.toFixed(2));
        var hook = _STD_.env["FORWARD_WEBHOOK"];
        if (hook) {
          httpPOST(hook, out, { "Content-Type": "application/json" },
            function () { print("forwarded " + src); },
            function (e) { print("forward failed " + e); });
        }
      }
    },
    function (e) { print("scan failed " + src + ": " + e); }
  );
}

function main() {
  var sources = (_STD_.env["SOURCES"] || "").split(",").map(function (s) { return s.trim(); }).filter(Boolean);
  if (!sources.length) { print("no SOURCES configured"); return; }
  sources.forEach(scan);
}

main();
