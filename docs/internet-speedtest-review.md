# Internet speed test validation

- Pinned official `@cloudflare/speedtest` 1.14.2 from npm; the reproducible vendoring tool verifies archive SHA-256.
  The original module and MIT license ship locally. See `third_party/README.txt` for hashes and provenance.
- Measurements are initiated by a click. Configured phases are latency, download and upload only. Optional
  measurement/result logging is disabled and no credentials, private files or monitor history are supplied.
- CSP permits connections only to self and the two Cloudflare transfer endpoint paths. Scripts remain local.
  Reaching the speed test server necessarily exposes the client's public IP and synthetic test traffic there.
- Buttons expose progress, cancellation and retry. Partial/failed/timed-out results are distinguished from
  completed measurements. A same-origin Web Lock prevents duplicate runs across supporting browser tabs.
- Five JavaScript checks cover opt-in behavior, real engine configuration, units, duplicate clicks, stale callbacks,
  cancellation/retry, failure/timeout, missing results and an occupied cross-tab lock. All pass.
- Twenty-five Python checks for static routing/CSP, onboarding, governor HTTP, monitoring and system metrics pass.
- The packaged Windows 0.5.0 executable was exercised in Chrome. A real test completed with positive download
  and upload measurements, latency, a completion timestamp and the retry button. Other resource cards and
  passive traffic monitoring continued to update. Raw measurements and screenshots remain local.
- The configured payload budget is up to 164.2 decimal MB before retries/headers. Adaptive completion can use less;
  this is a practical path measurement rather than a guarantee of the ISP plan's maximum line rate.
