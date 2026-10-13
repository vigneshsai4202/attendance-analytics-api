# DECISIONS.md

**1. Indexes.** Unique `emp_code` on `employees`, plus `(department, joined_on)` and `(joined_on, department)` for the headcount filters. On `attendance_logs`: unique `(emp_code, date)` (one record per day, and the punch-in race guard), `(date desc, emp_code)` for list sorting and month scans, and `(emp_code, punch_in desc)` for punch-out. `(date, status, emp_code)` is for status filters; I would drop it if `explain` shows it unused.

**2. Punch-in race.** Both requests call `insert_one` with the same `(emp_code, date)`. The unique index lets one insert succeed (201); the other raises `DuplicateKeyError`, returned as 409. There is no check-then-insert, so no window.

**3. Ties.** `$rank` gives tied employees the same rank (1, 2, 2, 4). The `rank <= limit` match runs after ranking, so every tied row is returned, possibly exceeding `limit`. Ties sort by `emp_code`.

**4. No-log employees.** The summary starts from `employees` joined by month end, then `$lookup`s logs and `$unwind`s with empty arrays preserved. Headcount is the size of the `emp_code` set, so they still count.

**5. 100x data.** The per-log `$lookup` into `employees` would hurt first. I would copy `department` onto each log and add monthly rollups, sharding only afterwards.