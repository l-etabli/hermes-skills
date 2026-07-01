import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import ingest_plan  # noqa: E402


class TruncateUtf8Test(unittest.TestCase):
    def test_no_truncation_below_budget(self):
        text, truncated = ingest_plan.truncate_utf8("héllo", 100)

        self.assertEqual(text, "héllo")
        self.assertFalse(truncated)

    def test_never_splits_a_multibyte_rune(self):
        # "é" is 2 bytes; budget of 3 lands mid-rune on the second "é".
        text, truncated = ingest_plan.truncate_utf8("éé", 3)

        self.assertEqual(text, "é")
        self.assertTrue(truncated)
        text.encode("utf-8")  # must not raise


class ParseReadsTest(unittest.TestCase):
    def test_filters_unknown_paths_and_dedupes(self):
        valid = {"log.md", "people/jerome.md"}

        reads, err = ingest_plan.parse_reads(
            '{"reads": ["log.md", "hallucinated.md", "log.md", "people/jerome.md"]}',
            valid,
        )

        self.assertIsNone(err)
        self.assertEqual(reads, ["log.md", "people/jerome.md"])

    def test_caps_at_max_reads(self):
        valid = {f"p{i}.md" for i in range(20)}

        reads, err = ingest_plan.parse_reads(
            json.dumps({"reads": [f"p{i}.md" for i in range(20)]}), valid,
        )

        self.assertIsNone(err)
        self.assertEqual(len(reads), ingest_plan.MAX_READS)

    def test_rejects_non_object_and_non_list(self):
        _, err = ingest_plan.parse_reads("[1, 2]", set())
        self.assertIsNotNone(err)

        _, err = ingest_plan.parse_reads('{"reads": "log.md"}', set())
        self.assertEqual(err, "reads-not-a-list")


class ParseEditPlanTest(unittest.TestCase):
    def test_happy_path_with_fence_and_rationale(self):
        plan = {
            "edits": [
                {"action": "append", "path": "log.md", "content": "- entry"},
                {"action": "patch", "path": "index.md", "find": "old", "replace_with": "new"},
                {"action": "create", "path": "concepts/x.md", "content": "# X"},
            ],
            "rationale": "log + index + nouvelle page",
        }

        edits, rationale, err = ingest_plan.parse_edit_plan(
            "```json\n" + json.dumps(plan) + "\n```"
        )

        self.assertIsNone(err)
        self.assertEqual(len(edits), 3)
        self.assertEqual(rationale, "log + index + nouvelle page")

    def test_empty_edits_is_valid(self):
        edits, rationale, err = ingest_plan.parse_edit_plan(
            '{"edits": [], "rationale": "smalltalk, rien à ingérer"}'
        )

        self.assertIsNone(err)
        self.assertEqual(edits, [])

    def test_rejects_bad_action_missing_content_and_missing_find(self):
        _, _, err = ingest_plan.parse_edit_plan(
            '{"edits": [{"action": "delete", "path": "log.md"}]}'
        )
        self.assertIn("bad-action", err)

        _, _, err = ingest_plan.parse_edit_plan(
            '{"edits": [{"action": "append", "path": "log.md", "content": "  "}]}'
        )
        self.assertIn("missing-content", err)

        _, _, err = ingest_plan.parse_edit_plan(
            '{"edits": [{"action": "patch", "path": "log.md", "replace_with": "x"}]}'
        )
        self.assertIn("patch-missing-find", err)

    def test_rejects_too_many_edits(self):
        edits = [{"action": "append", "path": "log.md", "content": "x"}] * (
            ingest_plan.MAX_EDITS + 1
        )

        _, _, err = ingest_plan.parse_edit_plan(json.dumps({"edits": edits}))

        self.assertIn("too-many-edits", err)


class DryRunEditsTest(unittest.TestCase):
    def _reader(self, files):
        return lambda path: files.get(path)

    def test_drops_patch_overlapping_an_earlier_patch(self):
        # Regression: live abort on 2026-07-01 — patch #2's find spanned
        # the line patch #1 had already rewritten in index.md.
        files = {"index.md": "- [[loic]] : Expert forage.\n- [[jerome]] : Lead dev.\n"}
        edits = [
            {"action": "patch", "path": "index.md",
             "find": "- [[loic]] : Expert forage.",
             "replace_with": "- [[loic]] : Expert forage et géothermie."},
            {"action": "patch", "path": "index.md",
             "find": "- [[loic]] : Expert forage.\n- [[jerome]] : Lead dev.",
             "replace_with": "nope"},
            {"action": "append", "path": "index.md", "content": "- [[well]]"},
        ]

        accepted, rejected = ingest_plan.dry_run_edits(edits, self._reader(files))

        self.assertEqual([e["action"] for e in accepted], ["patch", "append"])
        self.assertEqual(len(rejected), 1)
        self.assertIn("index.md", rejected[0])

    def test_patch_matching_text_added_by_earlier_edit_is_kept(self):
        files = {"log.md": "# Log\n"}
        edits = [
            {"action": "append", "path": "log.md", "content": "- new entry"},
            {"action": "patch", "path": "log.md",
             "find": "- new entry", "replace_with": "- new entry [[x]]"},
        ]

        accepted, rejected = ingest_plan.dry_run_edits(edits, self._reader(files))

        self.assertEqual(len(accepted), 2)
        self.assertEqual(rejected, [])

    def test_create_then_append_on_new_file(self):
        edits = [
            {"action": "create", "path": "concepts/x.md", "content": "# X"},
            {"action": "append", "path": "concepts/x.md", "content": "more"},
            {"action": "create", "path": "concepts/x.md", "content": "dup"},
        ]

        accepted, rejected = ingest_plan.dry_run_edits(edits, self._reader({}))

        self.assertEqual([e["action"] for e in accepted], ["create", "append"])
        self.assertIn("create but file exists", rejected[0])

    def test_ops_on_missing_file_are_dropped(self):
        edits = [
            {"action": "append", "path": "nope.md", "content": "x"},
            {"action": "patch", "path": "nope.md", "find": "a", "replace_with": "b"},
            {"action": "replace", "path": "nope.md", "content": "y"},
        ]

        accepted, rejected = ingest_plan.dry_run_edits(edits, self._reader({}))

        self.assertEqual(accepted, [])
        self.assertEqual(len(rejected), 3)


class BuildPlanMessagesTest(unittest.TestCase):
    def test_prompt_stays_under_argv_budget(self):
        files = {f"p{i}.md": "x" * 50_000 for i in range(ingest_plan.MAX_READS + 5)}

        messages = ingest_plan.build_plan_messages(
            "b" * 50_000, "raw/transcripts/t.md", "t" * 200_000, files,
        )

        total = sum(len(m["content"].encode("utf-8")) for m in messages)
        self.assertLess(total, 120_000)
        self.assertIn("raw/transcripts/t.md", messages[0]["content"])

    def test_truncated_file_carries_no_patch_warning(self):
        messages = ingest_plan.build_plan_messages(
            "brief", "raw/transcripts/t.md", "body",
            {"log.md": "x" * (ingest_plan.PLAN_FILE_BYTES + 1)},
        )

        self.assertIn("ne PAS le patcher", messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
