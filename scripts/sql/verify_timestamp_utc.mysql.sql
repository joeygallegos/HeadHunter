-- Read-only verification for the 20260717 JobScrape UTC migration.
-- The conversion itself is DST-aware Python; do not use fixed-offset SQL.

SELECT migration_id, status, applied_at_utc, restored_at_utc
FROM jobscrape_timestamp_migrations
WHERE migration_id = '20260717_normalize_timestamps_utc';

SELECT
    COUNT(*) AS analyzed_jobs,
    SUM(ai_analyzed_at < discovery_date) AS analyzed_before_discovery,
    MIN(TIMESTAMPDIFF(SECOND, discovery_date, ai_analyzed_at)) AS minimum_gap_seconds
FROM jobs
WHERE ai_analyzed_at IS NOT NULL;

SELECT COUNT(*) AS changed_discovery_dates
FROM jobs AS live
JOIN jobscrape_tz_20260717_jobs AS backup ON backup.id = live.id
WHERE NOT (live.discovery_date <=> backup.discovery_date);

-- Preferred rollback (restores every covered column from the backup tables):
-- python scripts/migrate_timestamps_to_utc.py --restore
