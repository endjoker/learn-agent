import json
import tempfile
import unittest
from pathlib import Path

from core.message_store import MessageStore


class MessageStoreProtocolTests(unittest.TestCase):
    def test_protocol_metadata_survives_save_and_load(self):
        store = MessageStore(session_id="roundtrip")
        store.append({"role": "system", "content": "internal"})
        store.append({
            "role": "assistant", "content": "envelope", "kind": "tool_calls",
            "tool_calls": [{"id": "one", "name": "read", "arguments": {}}],
        })
        store.append({"role": "user", "name": "tool_result", "content": "ok", "is_error": False})

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "session.json"
            returned = store.save_session(str(target))
            data = json.loads(target.read_text(encoding="utf-8"))
            restored = MessageStore()
            restored.load_session_data(data)

        self.assertEqual(returned, str(target))
        self.assertEqual(data["schema_version"], 2)
        self.assertEqual(len(data["messages"]), 2)  # system prompts are rebuilt, never persisted
        self.assertEqual(restored.messages[0]["kind"], "tool_calls")
        self.assertEqual(restored.messages[0]["tool_calls"][0]["name"], "read")
        self.assertEqual(restored.messages[1]["kind"], "tool_result")  # v1-compatible upgrade
        self.assertEqual(len(restored.get_tool_calls()), 1)

    def test_atomic_save_leaves_no_temporary_file(self):
        store = MessageStore(session_id="atomic")
        store.append({"role": "user", "content": "hello"})
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "atomic.json"
            store.save_session(str(target))
            self.assertTrue(target.exists())
            self.assertEqual(list(Path(directory).glob(".*.tmp")), [])
