import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


LLM_BACKEND_PATH = pathlib.Path(__file__).with_name("llm_backend.py")


def load_llm_backend():
    spec = importlib.util.spec_from_file_location("llm_backend_for_tests", LLM_BACKEND_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LlmBackendTest(unittest.TestCase):
    def test_extract_json_object_accepts_fenced_json(self):
        llm_backend = load_llm_backend()

        text, err = llm_backend.extract_json_object('```json\n{"ok": true}\n```')

        self.assertIsNone(err)
        self.assertEqual(json.loads(text), {"ok": True})

    def test_extract_json_object_ignores_surrounding_hermes_text(self):
        llm_backend = load_llm_backend()

        text, err = llm_backend.extract_json_object('Note: done.\n{"a": {"b": "}"}}\nFin')

        self.assertIsNone(err)
        self.assertEqual(json.loads(text), {"a": {"b": "}"}})

    def test_hermes_complete_text_calls_hermes_oneshot(self):
        llm_backend = load_llm_backend()
        with tempfile.TemporaryDirectory() as tmp:
            config = llm_backend.LlmBackendConfig(
                hermes_cli=pathlib.Path("/opt/hermes/.venv/bin/hermes"),
                cwd=pathlib.Path(tmp),
                timeout_s=123,
            )
            calls = []

            def fake_run(cmd, cwd, capture_output, text, timeout):
                calls.append({
                    "cmd": cmd,
                    "cwd": cwd,
                    "capture_output": capture_output,
                    "text": text,
                    "timeout": timeout,
                })
                return subprocess.CompletedProcess(cmd, 0, stdout='{"ok":true}\n', stderr="")

            content, err = llm_backend.hermes_complete_text(
                config,
                [{"role": "user", "content": "Réponds en JSON"}],
                "unit test",
                run_command=fake_run,
            )

            self.assertIsNone(err)
            self.assertEqual(content, '{"ok":true}')
            self.assertEqual(calls[0]["cmd"][:2], [str(config.hermes_cli), "-z"])
            self.assertIn("Réponds en JSON", calls[0]["cmd"][2])
            self.assertEqual(calls[0]["cwd"], str(pathlib.Path(tmp)))
            self.assertEqual(calls[0]["timeout"], 123)

if __name__ == "__main__":
    unittest.main()
