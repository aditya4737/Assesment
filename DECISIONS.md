# Design decisions

1. **Indexes.** Unique employee-code and `(emp_code, date)` indexes enforce both natural keys. `(date, emp_code)` supports date-filtered listing and analytics; `(emp_code, punch_in)` supports punch-out lookup; `(department, joined_on)` supports department rosters; and `joined_on` supports headcount queries. There is no `status` index because status has few distinct values and the other indexes cover the required filters and sort orders.

2. **Punch-in race.** Both requests may read the employee, but each inserts under the unique `(emp_code, date)` key. MongoDB accepts one; `DuplicateKeyError` maps the other to HTTP 409.

3. **Ties.** MongoDB `$rank` assigns equal totals the same competition rank. `limit` filters on rank, so all employees tied at the cutoff are returned. `emp_code` orders rows but does not break ties.

4. **Headcount.** Summary starts with employees joined by period end and outer-unwinds each employee's log lookup. Employees without logs still produce a grouped row and count.

5. **Scaling.** Measure the workload and query plans before adding infrastructure. If analytics become a bottleneck, evaluate materialized daily summaries and archival for older records, weighing refresh and write costs against read latency.
