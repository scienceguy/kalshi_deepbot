-- T19-07: expose existing latency and outbox storage under the canonical names.
CREATE VIEW IF NOT EXISTS latency_log AS
SELECT id, stage, duration_ms, event_id, venue, mode, created_at
FROM latency_measurements;

CREATE VIEW IF NOT EXISTS outbox AS
SELECT
    id,
    type AS topic,
    payload_json,
    created_at,
    delivered_at AS sent_at,
    attempts,
    last_error,
    event_id,
    status,
    venue,
    mode,
    next_attempt_at,
    webhook_path,
    claimed_at
FROM event_outbox;
