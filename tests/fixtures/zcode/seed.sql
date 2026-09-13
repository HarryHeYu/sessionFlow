-- Synthetic ~/.zcode/cli/db/db.sqlite (ZCode CLI store).
-- The DDL mirrors the real ZCode schema, constraints included, so a schema
-- drift breaks the zcode adapter test loudly. All data below is synthetic.
CREATE TABLE session (
    id text primary key, project_id text not null, workspace_id text,
    parent_id text, slug text not null, directory text not null, path text,
    title text not null, version text not null, share_url text,
    summary_additions integer, summary_deletions integer, summary_files integer,
    summary_diffs text, revert text, permission text,
    time_created integer not null, time_updated integer not null,
    time_compacting integer, time_archived integer,
    task_type text not null default 'interactive',
    title_source text not null default 'first_input'
      check(title_source in ('default', 'first_input', 'generated', 'custom')),
    title_message_id text, time_title_updated integer, trace_id text);

CREATE TABLE message (
    id text primary key,
    session_id text not null references session(id) on delete cascade,
    time_created integer not null, time_updated integer not null,
    data text not null, sequence integer);

CREATE TABLE part (
    id text primary key,
    message_id text not null references message(id) on delete cascade,
    session_id text not null,
    time_created integer not null, time_updated integer not null,
    data text not null, sequence integer);

CREATE TABLE model_usage (
    id text primary key, logical_request_id text not null,
    attempt_index integer not null default 0,
    session_id text not null references session(id) on delete cascade,
    turn_id text, trace_id text, span_id text, assistant_message_id text,
    parent_user_message_id text, query_source text not null,
    provider_id text not null, model_id text not null, variant text,
    agent text, mode text, task_type text,
    status text not null check(status in ('running','completed','error','cancelled')),
    started_at integer not null, first_token_at integer, completed_at integer,
    duration_ms integer, time_to_first_token_ms integer, finish_reason text,
    tool_call_count integer not null default 0,
    input_tokens integer not null default 0,
    output_tokens integer not null default 0,
    reasoning_tokens integer not null default 0,
    cache_creation_input_tokens integer not null default 0,
    cache_read_input_tokens integer not null default 0,
    provider_total_tokens integer,
    computed_total_tokens integer not null default 0,
    retry_count integer not null default 0,
    retryable integer not null default 0 check(retryable in (0,1)),
    cancelled_by_user integer not null default 0 check(cancelled_by_user in (0,1)),
    context_exceeded integer not null default 0 check(context_exceeded in (0,1)),
    error_type text, error_code text, error_message text,
    raw_usage_json text, provider_metadata_json text);

CREATE TABLE tool_usage (
    id text primary key,
    session_id text not null references session(id) on delete cascade,
    turn_id text, trace_id text,
    tool_call_id text not null, tool_name text not null,
    side_effect_scope text,
    read_only integer check(read_only in (0,1)),
    destructive integer check(destructive in (0,1)),
    approval_status text,
    status text not null check(status in ('running','completed','error','cancelled')),
    started_at integer not null, first_output_at integer, completed_at integer,
    duration_ms integer, time_to_first_output_ms integer, exit_code integer,
    output_bytes integer not null default 0,
    stdout_bytes integer not null default 0,
    stderr_bytes integer not null default 0,
    truncated integer not null default 0 check(truncated in (0,1)),
    retry_count integer not null default 0,
    retryable integer not null default 0 check(retryable in (0,1)),
    cancelled_by_user integer not null default 0 check(cancelled_by_user in (0,1)),
    error_type text, error_code text, error_message text);

INSERT INTO session(id, project_id, slug, directory, title, version,
    summary_additions, summary_deletions, summary_files,
    time_created, time_updated, task_type, title_source)
VALUES ('sess_z1', 'proj_x', 'sess_z1', 'E:/proj/demo', 'zcode title',
    '1.2.3', 12, 3, 4, 1757000000000, 1757000100000, 'interactive', 'first_input');

INSERT INTO message(id, session_id, time_created, time_updated, data, sequence)
VALUES ('m1', 'sess_z1', 1757000000000, 1757000000000,
    '{"role": "user", "time": {"created": 1757000000000}, "path": {"cwd": "E:/proj/demo", "root": "E:/proj/demo"}, "model": {"providerID": "p", "modelID": "m"}}',
    1);

INSERT INTO message(id, session_id, time_created, time_updated, data, sequence)
VALUES ('m2', 'sess_z1', 1757000005000, 1757000005000,
    '{"role": "assistant", "time": {"created": 1757000005000}}', 2);

INSERT INTO part(id, message_id, session_id, time_created, time_updated, data, sequence)
VALUES ('p0', 'm1', 'sess_z1', 1757000000000, 1757000000000,
    '{"type": "text", "text": "zcode 你好"}', 0);
INSERT INTO part(id, message_id, session_id, time_created, time_updated, data, sequence)
VALUES ('p1', 'm1', 'sess_z1', 1757000001000, 1757000001000,
    '{"type": "reasoning", "text": "思考中"}', 1);
INSERT INTO part(id, message_id, session_id, time_created, time_updated, data, sequence)
VALUES ('p2', 'm2', 'sess_z1', 1757000005000, 1757000005000,
    '{"type": "tool", "callID": "zc1", "tool": "Bash", "state": {"status": "completed", "input": {"command": "ls"}, "output": "files..."}}',
    2);
INSERT INTO part(id, message_id, session_id, time_created, time_updated, data, sequence)
VALUES ('p3', 'm2', 'sess_z1', 1757000006000, 1757000006000,
    '{"type": "text", "text": "列完了"}', 3);

INSERT INTO tool_usage(id, session_id, tool_call_id, tool_name, status,
    started_at, completed_at, exit_code, stdout_bytes, stderr_bytes,
    approval_status, read_only, destructive)
VALUES ('tu1', 'sess_z1', 'zc1', 'Bash', 'completed', 1757000005000,
    1757000006000, 0, 42, 0, 'approved', 1, 0);

INSERT INTO model_usage(id, logical_request_id, session_id, query_source,
    provider_id, model_id, status, started_at, input_tokens, output_tokens,
    reasoning_tokens, cache_read_input_tokens, cache_creation_input_tokens,
    computed_total_tokens)
VALUES ('mu0', 'lr0', 'sess_z1', 'chat', 'anthropic', 'claude-sonnet',
    'completed', 1757000000000, 120, 40, 0, 10, 0, 160);

INSERT INTO model_usage(id, logical_request_id, session_id, query_source,
    provider_id, model_id, status, started_at, input_tokens, output_tokens,
    reasoning_tokens, cache_read_input_tokens, cache_creation_input_tokens,
    computed_total_tokens)
VALUES ('mu1', 'lr1', 'sess_z1', 'chat', 'anthropic', 'claude-sonnet',
    'completed', 1757000000001, 80, 20, 0, 10, 0, 100);
