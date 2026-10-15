# DECISIONS.md

1. **Indexes.** Unique `emp_code` on employees (duplicate-create guarantee, lookups); unique `(emp_code, date)` on attendance (one record per day, monthly queries); `(date desc, emp_code)` for the list sort; `(date, emp_code)` for date-range analytics; `(status, date desc, emp_code)` for status-filtered lists; `(department, joined_on)` for department queries. I rejected `punch_in`: punch-out already narrows to one employee via the unique index, so it would only cost writes.

2. **Punch-in race.** Both requests look the employee up and compute the same date. Neither checks for an existing record. Both attempt the insert; the unique index lets exactly one succeed with 201, the other raises `DuplicateKeyError`, which I turn into 409.

3. **Ties.** `$rank` in `$setWindowFields` gives tied employees the same rank, and the `limit` filter (`rank <= limit`) runs after ranking. So employees tied at the cutoff are all returned, even if that exceeds `limit` rows; cutting by row count would arbitrarily drop an equal.

4. **Headcount.** The pipeline starts from `employees` with `joined_on <=` month end, `$lookup`s that month's logs, and unwinds with `preserveNullAndEmptyArrays`. Log-less employees survive, and headcount counts distinct `emp_code`s per department; late joiners never enter the pipeline.

5. **One thing for 100x data.** Precompute daily rollups per employee and department, updated on every punch and correction, so analytics read small rollups instead of scanning raw logs.
