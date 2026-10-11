# DECISIONS.md

## 1. Indexes

**Question:** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?

**Answer:**
- **Employees:** A unique `emp_code` index ensures employee identity, while `(department, emp_code)` supports sorted department listings.
- **Attendance:** A unique `(emp_code, date)` index prevents duplicate daily records. `(date DESC, emp_code)` supports attendance listing, and `(status, date DESC)` supports status/date filters and analytics.
- **Rejected index:** I considered a standalone `joined_on` index but rejected it because the current eligibility predicate matches nearly all employees. It would add storage and write costs without a measurable benefit.

## 2. Punch-in Race Condition

**Question:** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.

**Answer:**
- Both requests may pass the preliminary `find_one` check.
- MongoDB's unique attendance index permits only one insert for the same employee and date.
- The losing request raises `DuplicateKeyError`, which is converted to HTTP `409 Conflict`.
- Exactly one attendance record remains.

## 3. Leaderboard Ties

**Question:** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?

**Answer:**
- The leaderboard uses MongoDB's `$rank` rather than limiting rows before ranking.
- Every employee tied at the cutoff rank is returned, even if the result contains more rows than the requested `limit`.

## 4. Department Headcount

**Question:** How does your department summary make sure employees with zero attendance logs are counted?

**Answer:**
- The department summary starts with eligible employee documents and looks up their attendance logs.
- Employees without matching logs remain in the aggregation pipeline and still contribute to the department headcount.

## 5. Scaling to 100x Data

**Question:** What is one thing you would change if this had to serve 100 times the current data volume?

**Answer:**
- I would first measure query plans and latency.
- Based on the results, I would consider partitioning or archiving attendance data by date and reviewing aggregation indexes.
