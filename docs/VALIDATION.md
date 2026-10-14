# Validation

Checked on 14 October 2026. [Machine-readable test results](test-results.json) record the executed suite and application/test hashes.

The completed suite passed **10 tests** with no failures or errors. Tests use persistent fictional application records and exercise an actual post-write worker exit, duplicate concurrent requests, approval bypass refusal, stale policy grants, exact account permissions, source drift, and checker write separation. The model is not evaluated by this correctness suite.

## Recorded evidence

[The edited 1:53 reference-mode video](../submission/demo.mp4) shows a CRM-only reapproval, saved Directory account after worker exit, one reference reconciliation, four checked application accounts, and detection and correction of an extra admin permission. It uses synthetic narration over actual browser capture.

One separate live Claude interpretation returned the approved requested role with tools disabled and no fallback. This is a successful smoke check, not a measured accuracy rate. The video itself does not contain a live inference call.

Desktop and mobile views were inspected for navigation, JavaScript errors and horizontal overflow. The cover is generated concept art; workspace screenshots and video are actual browser captures. The saved-request selector was added after recording.

Run `python -m unittest discover -s tests -v`. The development manager controls are not a production authorization layer. No real workplace connector, external permission grant, production identity boundary or comparison against other submissions was evaluated.

An isolated browser smoke check created two requests, switched between saved requests, and confirmed that a running worker continued against its original employee. Starting another request does not reset saved records.
