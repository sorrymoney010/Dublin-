/**
 * Acurast Monitor-as-a-Service  (Engine 4 — Product A)
 * Runs ON the processor. Polls customer URLs and POSTs a failure alert to a
 * webhook. Uses the documented processor runtime API only
 * (print, httpGET, httpPOST, _STD_.env).
 */
function monitorOne(url) {
  print("checking " + url);
  httpGET(
    url,
    { "Accept": "application/json, text/plain, */*" },
    function (payload, cert) {
      print("OK " + url + " (len=" + payload.length + ")");
    },
    function (errMsg) {
      var body = JSON.stringify({
        url: url,
        status: "DOWN",
        error: errMsg,
        ts: Date.now(),
        processor: _STD_.device.getAddress(),
      });
      print("ALERT " + url + " -> " + errMsg);
      var hook = _STD_.env["ALERT_WEBHOOK"];
      if (hook) {
        httpPOST(
          hook,
          body,
          { "Content-Type": "application/json" },
          function () { print("alert delivered " + url); },
          function (e) { print("alert post failed " + e); }
        );
      }
    }
  );
}

function main() {
  var targets = (_STD_.env["TARGET_URL"] || "").split(",").map(function (s) { return s.trim(); }).filter(Boolean);
  if (!targets.length) {
    print("no TARGET_URL configured");
    return;
  }
  targets.forEach(monitorOne);
}

main();
