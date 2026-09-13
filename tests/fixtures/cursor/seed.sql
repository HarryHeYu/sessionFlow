-- Synthetic AppData/Roaming/Cursor/User/globalStorage/state.vscdb.
-- Only the key-value table the Cursor adapter reads; data is synthetic.
CREATE TABLE cursorDiskKV (key TEXT, value TEXT);

INSERT INTO cursorDiskKV VALUES ('composerData:cur-1',
  '{"composerId": "cur-1", "name": "cursor demo", "createdAt": 1757000000000, "modelConfig": {"modelName": "gpt-x"}, "trackedGitRepos": ["E:/proj/demo"], "fullConversationHeadersOnly": [{"bubbleId": "b1"}, {"bubbleId": "b2"}, {"bubbleId": "b3"}]}');

INSERT INTO cursorDiskKV VALUES ('bubbleId:cur-1:b1',
  '{"type": 1, "text": "cursor 你好", "createdAt": 1757000000000}');

INSERT INTO cursorDiskKV VALUES ('bubbleId:cur-1:b2',
  '{"type": 2, "text": "好的", "createdAt": 1757000001000, "toolFormerData": {"toolCallId": "cb1", "name": "read_file", "rawArgs": "{\"path\":\"a.py\"}", "result": "{\"contents\":\"body\"}", "status": "completed"}}');

INSERT INTO cursorDiskKV VALUES ('bubbleId:cur-1:b3',
  '{"type": 2, "text": "完成", "createdAt": 1757000002000}');
