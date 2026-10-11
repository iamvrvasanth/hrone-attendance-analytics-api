# DECISIONS.md

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?

1. **Indexes.** Employees use unique `emp_code` for identity and a
`(department, emp_code)` index for sorted department listing. Attendance uses
unique `(emp_code, date)` to prevent duplicate daily records, `(date DESC,
emp_code)` for listing, and `(status, date DESC)` for status/date filters and
analytics. I considered a standalone `joined_on` index, but did not add it:
the current eligibility predicate matches nearly all employees and would add
write/storage cost without measurable benefit.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.

2. **Punch-in race.** Both requests may pass the preliminary `find_one`.
MongoDB's unique attendance index allows only one insert. The losing
`DuplicateKeyError` is converted to HTTP 409, so exactly one record remains.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?

3. **Ties.** The leaderboard uses MongoDB `$rank`, not a row limit before
ranking. Every employee tied at the cutoff rank is returned, even if that
means more than `limit` rows.

4. **Headcount.** How does your department summary make sure employees with zero attendance logs are counted?

4. **Headcount.** Department summary starts from eligible employee documents
and looks up attendance logs. Employees with no matching logs therefore remain
in the pipeline and contribute to headcount.

5. **One thing you would change** if this had to serve 100x the data.

5. **100x scale.** I would first measure query plans and latency, then
partition or archive attendance data by date and review aggregation indexes.
