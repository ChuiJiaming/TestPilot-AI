-- Read-only mismatch report for db/migrations/005_scope_linkage.sql.
-- Require all counts to be zero before separately validating constraints.
SELECT 'case_versions_wrong_project' AS issue, count(*) AS count
FROM test_case_versions v
LEFT JOIN test_cases c ON c.id = v.case_id
WHERE v.project_id IS DISTINCT FROM c.project_id
UNION ALL
SELECT 'plans_wrong_suite_project', count(*)
FROM test_plans p
LEFT JOIN test_suites s ON s.id = p.suite_id
WHERE p.suite_id IS NOT NULL AND p.project_id IS DISTINCT FROM s.project_id
UNION ALL
SELECT 'run_cases_wrong_run_project', count(*)
FROM run_cases rc
LEFT JOIN test_runs r ON r.id = rc.run_id
WHERE rc.project_id IS DISTINCT FROM r.project_id
UNION ALL
SELECT 'run_cases_wrong_version_project', count(*)
FROM run_cases rc
LEFT JOIN test_case_versions v ON v.id = rc.case_version_id
WHERE rc.project_id IS DISTINCT FROM v.project_id
UNION ALL
SELECT 'visual_baselines_wrong_version_project', count(*)
FROM visual_baselines vb
LEFT JOIN test_case_versions v ON v.id = vb.case_version_id
WHERE vb.project_id IS DISTINCT FROM v.project_id;
