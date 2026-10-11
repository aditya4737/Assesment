# Implementation review

| Area | Issue | Implementation |
|---|---|---|
| Time calculations | Late minutes used a truncated grace-period comparison, and overnight shifts were not handled consistently. Work-hour rounding also relied on floating-point rounding. | Shift boundaries are evaluated in IST, timestamps are truncated to whole seconds, and durations use decimal half-up rounding. Overtime accounts for overnight shifts and the 30-minute threshold. |
| Startup and health | Duplicate employee codes and duplicate attendance dates were not protected at the database level. The health endpoint did not verify MongoDB connectivity. | Startup creates named unique and query-supporting indexes. The health endpoint pings MongoDB and returns 503 when it is unavailable. |
| Employee endpoints | Employee input was insufficiently validated, duplicate checks could race, and pagination totals and offsets were incorrect. | Request fields and dates are validated; the unique index handles concurrent duplicate creates. Employee listing applies the department filter to both the query and total, uses a stable sort, and starts page offsets at the correct record. |
| Attendance endpoints | Missing employees could cause server errors, timestamps and attendance dates did not consistently follow the API contract, and attendance listing loaded matching records before paging. | Unknown employees return 404, timestamps use epoch milliseconds at the API boundary, and attendance dates follow IST shift rules. Listing filters, sorts, counts, and pages in MongoDB. |
| Punch-out and corrections | Punch-out, regularization history, analytics, and explain routes were incomplete. | The remaining contract routes are implemented. Punch-out and corrections use conditional updates to detect conflicting writes; corrections append audit history and recompute derived fields. |

The time-boundary cases worth checking include the end of the 10-minute late grace period, overnight shifts, and the 30-minute overtime threshold. MongoDB-dependent routes require a reachable database.
