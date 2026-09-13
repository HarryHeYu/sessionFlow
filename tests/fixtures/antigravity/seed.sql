-- Synthetic ~/.gemini/antigravity/conversations/ag-1.db.
-- steps.step_payload is protobuf without a public schema; the adapter pulls
-- printable UTF-8 runs out of the blobs, so these payloads are plain strings
-- cast to BLOB: 14=init/prompt, 15=message, 132=tool call, 101=task done,
-- 17=error. Data is synthetic.
CREATE TABLE steps (idx INTEGER PRIMARY KEY,
    step_type INTEGER, status INTEGER, has_subtrajectory INTEGER,
    metadata TEXT, error_details TEXT, permissions TEXT,
    task_details TEXT, render_info TEXT, step_payload BLOB,
    step_format INTEGER);

INSERT INTO steps VALUES (0, 14, 2, 0, NULL, NULL, NULL, NULL, NULL,
    CAST('帮我把项目重构一遍 git@github.com:u/demo.git
working folder E:/proj/demo' AS BLOB), 0);

INSERT INTO steps VALUES (1, 15, 2, 0, NULL, NULL, NULL, NULL, NULL,
    CAST('assistant message body xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' AS BLOB), 0);

INSERT INTO steps VALUES (2, 132, 2, 0, NULL, NULL, NULL, NULL, NULL,
    CAST('toolu_vrtx_1
run_command
{"command":"python build.py"}' AS BLOB), 0);

INSERT INTO steps VALUES (3, 101, 2, 0, NULL, NULL, NULL, NULL, NULL,
    CAST('The command exited with code 0 and finished' AS BLOB), 0);

INSERT INTO steps VALUES (4, 17, 2, 0, NULL, NULL, NULL, NULL, NULL,
    CAST('HTTP 400 error happened here' AS BLOB), 0);
