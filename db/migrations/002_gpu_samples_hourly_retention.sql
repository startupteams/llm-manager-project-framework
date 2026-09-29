-- 002_gpu_samples_hourly_retention: GPU telemetry Option A (authorized by Jordan, 2026-09-29).
--
-- Raw retention:    90 days  (config: manager_settings key gpu_samples_retention_days, default 90)
-- Hourly retention: 730 days (config: manager_settings key gpu_samples_hourly_retention_days, default 730)
-- Partitioning:     DEFERRED (TDR-0009) until size/ingest/query evidence demands it.
--
-- NEVER prune raw before the hourly rollup exists, is backfilled, and is verified.
-- This migration creates ONLY the rollup table + indexes. Pruning lives in
-- service/app/gpu_retention.py behind verified preconditions.
--
-- Rollup grain: (host_id, gpu_index, hour_bucket). Metrics chosen to preserve
-- every statistic the app currently derives from gpu_samples:
--   - latest per-GPU util/power  (main.py:474 latest-24-rows query)
--   - per-host avg util for spend model (main.py:638 LEFT JOIN avg util_pct)
--   - 5-minute collector health (main.py:494 recent-ts count)
-- plus min/max for capacity planning. NULL-safe: empty hours are not emitted.

CREATE TABLE IF NOT EXISTS gpu_samples_hourly (
    hour_bucket   timestamptz NOT NULL,          -- date_trunc('hour', ts)
    host_id       integer     NOT NULL,
    gpu_index     integer     NOT NULL,
    sample_count  integer     NOT NULL,
    util_min      numeric,
    util_max      numeric,
    util_avg      numeric,
    mem_used_min_mib numeric,
    mem_used_max_mib numeric,
    mem_used_avg_mib numeric,
    power_min_w   numeric,
    power_max_w   numeric,
    power_avg_w   numeric,
    power_sum_wh  numeric,                       -- sum(watts)/samples integrated: avg_w * (count * sample_interval) is lossy; sum of watts is the raw material
    last_ts       timestamptz NOT NULL,
    PRIMARY KEY (hour_bucket, host_id, gpu_index)
);
ALTER TABLE gpu_samples_hourly OWNER TO llmmanager;

CREATE INDEX IF NOT EXISTS idx_gpu_hourly_host_ts
    ON gpu_samples_hourly (host_id, hour_bucket DESC);

-- Seed the two retention knobs as configuration-backed values (plan §B5).
-- ON CONFLICT keeps operator overrides intact on re-apply.
INSERT INTO manager_settings (key, value)
VALUES
    ('gpu_samples_retention_days', '90'),
    ('gpu_samples_hourly_retention_days', '730')
ON CONFLICT (key) DO NOTHING;
