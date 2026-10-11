# Review Report

The following defects were found in the starter implementation and corrected.

| # | Where | What was wrong | Detection | Fix |
|---|---|---|---|---|
| 1 | `health()` (current `app/main.py:797-811`) | `/health` reported success without checking MongoDB. | Simulate an unavailable database and observe a false healthy response. | Ping MongoDB and return HTTP 503 with an unavailable response on failure. |
| 2 | `list_employees()` (current `app/main.py:859-884`) | Pagination skipped `page * page_size` records, and totals ignored the department filter. | Page 1 omitted the first page; filtered totals included other departments. | Use `(page - 1) * page_size`, apply the same filter to `count_documents`, and validate page bounds. |
| 3 | `compute_late_minutes()` (current `app/main.py:570-606`) | The grace-period decision could be made after losing sub-minute information. | A punch at 09:40:01 for a 09:30 shift exposed the boundary error. | Compare elapsed seconds to the 10-minute grace period before flooring to whole minutes. |
| 4 | `_to_utc_datetime()`, `_as_ist_second()`, and timestamp request validators (current `app/main.py:211-256`, `541-568`) | Incoming instants were not consistently validated and timezone conversion could depend on local behavior. | Invalid epoch values, timezone-boundary inputs, and omitted/falsey timestamps produced incorrect handling. | Validate epoch milliseconds, convert through UTC explicitly, use IST for business rules, and normalize calculation instants to whole seconds. |
| 5 | `punch_in()` and `create_indexes()` (current `app/main.py:914-995`, `137-171`) | Check-then-insert alone was not safe for simultaneous punch-ins. | Parallel identical requests could both pass the existence check. | Add the unique `(emp_code, date)` index and convert the losing `DuplicateKeyError` to HTTP 409. |
| 6 | `list_attendance()` (current `app/main.py:1130-1181`) | The starter retrieved, sorted, and paginated records in Python. | The approach scales with all matching records instead of one page. | Filter, sort, count, skip, and limit in MongoDB. |
| 7 | `compute_work_hours()` and `compute_overtime()` (current `app/main.py:609-690`) | Built-in rounding and shift-end handling did not implement contract half-up rounding, the overtime threshold, or overnight shifts correctly. | Half-cent boundaries, short overtime, and overnight examples produced wrong values. | Use `Decimal` with `ROUND_HALF_UP`, enforce the 30-minute overtime threshold, and place overnight shift ends on the next date. |
| 8 | `create_indexes()`, attendance serializers, and endpoint writes (current `app/main.py:137-194`, `278-296`) | The starter lacked required database guarantees and exposed internal attendance identifiers. | Duplicate data, missing indexes, and response fields could be observed in integration behavior. | Create the required named indexes, preserve atomic updates/duplicate handling, and serialize only contract fields without MongoDB `_id`. |

## Reviewed behavior that was not a defect

- `joined_on` remains a calendar date string; it is not converted to an
  instant.
- Attendance analytics read stored `late_minutes`, `overtime_minutes`, and
  `half_day` values rather than recomputing them from punch timestamps.
- Punch-out selects an open record whose `punch_in` is no later than the
  requested timestamp, preserves the 24-hour maximum, and uses an atomic
  update. Exactly 24 hours remains valid.
- Employee creation uses the unique `emp_code` index and catches the
  insert-time `DuplicateKeyError` race.
- Regularization uses an optimistic concurrency predicate and records the
  prior values in history.
- Analytics calculations remain MongoDB aggregation pipelines; Python does not
  loop over attendance records to calculate summaries.
