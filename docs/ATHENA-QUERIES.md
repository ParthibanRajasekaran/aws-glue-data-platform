# Athena Queries

All queries run against `hr_analytics.employees` via the `hr_analytics_wg` workgroup.
The 100 MB scan guardrail is enforced at the workgroup level - queries that would exceed it are cancelled automatically.

Partitions are registered automatically by the Glue job after each run (`enableUpdateCatalog=True`), so no `MSCK REPAIR TABLE` is needed before querying.

---

## Employees flagged for compensation review

```sql
SELECT
    employeeid,
    firstname,
    lastname,
    jobtitle,
    departmentname,
    salary,
    comparatio
FROM hr_analytics.employees
WHERE requiresreview = true
ORDER BY comparatio DESC;
```

`requiresreview` is true when `comparatio > 1.0` (salary exceeds the band maximum) or when the assigned manager is inactive. Both conditions are computed in the Glue job and stored as attributes.

---

## Average salary and headcount by department

```sql
SELECT
    departmentname,
    ROUND(AVG(salary), 0) AS avg_salary,
    COUNT(*) AS headcount,
    ROUND(AVG(comparatio), 2) AS avg_comparatio
FROM hr_analytics.employees
GROUP BY departmentname
ORDER BY avg_salary DESC;
```

---

## Top earners relative to their job title band

```sql
SELECT
    firstname,
    lastname,
    jobtitle,
    salary,
    highesttitlesalary,
    ROUND((salary / highesttitlesalary) * 100, 1) AS pct_of_title_max
FROM hr_analytics.employees
ORDER BY pct_of_title_max DESC
LIMIT 20;
```

`highesttitlesalary` is computed in the Glue job as a window function: `MAX(salary) OVER (PARTITION BY jobtitle)`. Comparing an individual's salary to the title ceiling shows compression risk without needing a separate salary band table.

---

## Hiring trend by department and year

```sql
SELECT
    year,
    departmentname,
    COUNT(*) AS hires
FROM hr_analytics.employees
GROUP BY year, departmentname
ORDER BY year DESC, hires DESC;
```

This query benefits directly from Hive partitioning - filtering by `year` prunes all partitions outside that year before scanning begins.

---

## Partition-aware cost check before running a broad query

```sql
-- Check how many partitions exist before a full scan
SELECT DISTINCT year, month, dept
FROM hr_analytics.employees
ORDER BY year, month;
```
