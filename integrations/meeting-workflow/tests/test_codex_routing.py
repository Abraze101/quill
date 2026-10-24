import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quill_workflow.codex_routing import run, validate_result
from quill_workflow.pipeline import WorkflowError, atomic_json


class CodexRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.transcript = self.root / "transcript.json"
        self.source = {"segments": [
            {"text": "This meeting is about Acme's own recruitment plan."},
            {"text": "Beta is one of Acme's vendors."}]}
        atomic_json(self.transcript, self.source)
        self.companies = self.root / "companies.json"
        self.company_list = [
            {"id": "acme", "name": "Acme", "description": "Provides recruitment services.",
             "drive_folder_id": "PRIVATE_ACME_FOLDER"},
            {"id": "beta", "name": "Beta", "description": "Provides office supplies.",
             "drive_folder_id": "PRIVATE_BETA_FOLDER"}]
        atomic_json(self.companies, self.company_list)
        self.output = self.root / "suggestion.json"
        self.suggestion = {"schema_version": 1, "company_id": "acme", "confidence": "high",
                           "reason": "The meeting explicitly concerns Acme's own work.",
                           "evidence": [0], "ambiguous": False}

    def tearDown(self):
        self.temp.cleanup()

    def fake(self, code):
        path = self.root / "fake codex"
        path.write_text("#!" + sys.executable + "\nimport sys, json, os, time\nfrom pathlib import Path\n" + code)
        path.chmod(0o700)
        return str(path)

    def answer(self, data):
        return self.fake("sys.stdin.read()\nPath(sys.argv[sys.argv.index('--output-last-message')+1])"
                         ".write_text(" + repr(json.dumps(data)) + ")\n")

    def invoke(self, executable, **options):
        return run(self.transcript, self.companies, self.output, executable, **options)

    def test_supplied_only_prompt_and_private_output(self):
        capture = self.root / "capture.json"
        executable = self.fake(
            "payload=json.loads(sys.stdin.read())\n"
            "Path(" + repr(str(capture)) + ").write_text(json.dumps({'payload':payload,'argv':sys.argv,"
            "'cwd':os.getcwd(),'instructions':Path('instructions.txt').read_text(),"
            "'schema':json.loads(Path('schema.json').read_text()),"
            "'has_api_key':any(k in os.environ for k in ['OPENAI_API_KEY','ANTHROPIC_API_KEY'])}))\n"
            "Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text("
            + repr(json.dumps(self.suggestion)) + ")\n")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake", "ANTHROPIC_API_KEY": "fake"}):
            self.invoke(executable)
        call = json.loads(capture.read_text())
        self.assertEqual(json.loads(self.output.read_text()), self.suggestion)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertEqual(call["payload"]["transcript"], self.source)
        self.assertEqual([set(c) for c in call["payload"]["companies"]],
                         [{"id", "name", "description"}] * 2)
        self.assertNotIn("PRIVATE_", json.dumps(call))
        self.assertNotIn(str(self.transcript), call["argv"])
        self.assertNotIn(str(self.companies), call["argv"])
        self.assertNotIn("--model", call["argv"])
        self.assertFalse(call["has_api_key"])
        self.assertFalse(Path(call["cwd"]).exists())
        for flag in ("--ignore-user-config", "--ignore-rules", "--strict-config", "--ephemeral"):
            self.assertIn(flag, call["argv"])
        self.assertIn('forced_login_method="chatgpt"', call["argv"])
        for feature in ("shell_tool", "unified_exec", "apps", "plugins", "hooks", "multi_agent"):
            self.assertEqual(call["argv"][call["argv"].index(feature) - 1], "--disable")
        self.assertIn("customer, vendor, competitor", call["instructions"])
        self.assertIn("Do not infer a business description from a company name", call["instructions"])
        self.assertIn("explicit organizational ownership", call["instructions"])
        self.assertEqual(call["schema"]["properties"]["company_id"]["enum"], ["acme", "beta", None])
        self.assertEqual(call["schema"]["properties"]["schema_version"]["const"], 2)
        self.assertIn("explicit_basis", call["schema"]["required"])
        self.assertIn("entire", call["instructions"])
        self.assertIn("Use only the aliases supplied with each", call["instructions"])
        self.assertNotIn("Known spelling variants", call["instructions"])

    def test_only_configured_aliases_reach_model_without_destination_ids(self):
        self.company_list[0]["aliases"] = ["Ackme", "ACKME"]
        atomic_json(self.companies, self.company_list)
        capture = self.root / "alias-prompt.json"
        executable = self.fake(
            "payload=json.loads(sys.stdin.read())\n"
            "Path(" + repr(str(capture)) + ").write_text(json.dumps(payload))\n"
            "Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text("
            + repr(json.dumps(self.suggestion)) + ")\n")
        self.invoke(executable)
        prompt = json.loads(capture.read_text())
        expected = [{key: company[key] for key in ("id", "name", "description")}
                    for company in self.company_list]
        expected[0]["aliases"] = ["ackme"]
        self.assertEqual(prompt["companies"], expected)
        self.assertNotIn("PRIVATE_", capture.read_text())
        self.assertNotIn("drive_folder_id", capture.read_text())

    def test_missing_descriptions_allow_explicit_context_or_abstention(self):
        for company in self.company_list:
            company.pop("description")
        atomic_json(self.companies, self.company_list)
        self.invoke(self.answer(self.suggestion))
        self.assertEqual(json.loads(self.output.read_text()), self.suggestion)
        self.output.unlink()
        abstention = dict(self.suggestion, company_id=None, confidence="low", ambiguous=True,
                          evidence=[], reason="The transcript does not establish which company owns the work.")
        self.invoke(self.answer(abstention))
        self.assertEqual(json.loads(self.output.read_text()), abstention)

    def test_invalid_or_invented_model_output_is_not_published(self):
        invalid = [dict(self.suggestion, company_id="invented"),
                   dict(self.suggestion, company_id="PRIVATE_ACME_FOLDER"),
                   dict(self.suggestion, drive_folder_id="PRIVATE_ACME_FOLDER"),
                   dict(self.suggestion, evidence=[99]), dict(self.suggestion, evidence=[True]),
                   dict(self.suggestion, evidence=[0, 0]), dict(self.suggestion, evidence=[]),
                   dict(self.suggestion, confidence="low"), dict(self.suggestion, ambiguous=True),
                   dict(self.suggestion, schema_version=True), dict(self.suggestion, reason=" "),
                   dict(self.suggestion, company_id=None),
                   dict(self.suggestion, company_id=None, confidence="high", ambiguous=True)]
        for suggestion in invalid:
            with self.subTest(suggestion=suggestion):
                with self.assertRaises(WorkflowError):
                    self.invoke(self.answer(suggestion))
                self.assertFalse(self.output.exists())

    def test_medium_confidence_is_valid_but_remains_a_suggestion(self):
        suggestion = dict(self.suggestion, confidence="medium")
        validate_result(suggestion, self.source, self.company_list)
        self.invoke(self.answer(suggestion))
        self.assertEqual(json.loads(self.output.read_text()), suggestion)

    def test_version_two_retains_exact_explicit_evidence_and_supports_no_basis(self):
        basis = {"kind": "ownership", "segment_index": 0, "quote": self.source["segments"][0]["text"]}
        suggestion = dict(self.suggestion, schema_version=2, explicit_basis=basis)
        self.invoke(self.answer(suggestion))
        self.assertEqual(json.loads(self.output.read_text()), suggestion)
        self.output.unlink()
        suggestion["explicit_basis"] = None
        self.invoke(self.answer(suggestion))
        self.assertEqual(json.loads(self.output.read_text()), suggestion)

    def test_version_two_rejects_fabricated_shortened_and_malformed_evidence(self):
        basis = {"kind": "ownership", "segment_index": 0, "quote": self.source["segments"][0]["text"]}
        for changes in ({"quote": "Acme's own recruitment plan."}, {"quote": "This is an Acme call."},
                        {"segment_index": 1}, {"segment_index": True}, {"kind": "inferred"},
                        {"destination": "PRIVATE_ACME_FOLDER"}):
            with self.subTest(changes=changes), self.assertRaises(WorkflowError):
                self.invoke(self.answer(dict(self.suggestion, schema_version=2,
                                             explicit_basis=dict(basis, **changes))))
            self.assertFalse(self.output.exists())

    def test_general_is_a_configured_category_with_evidence_not_an_unknown_fallback(self):
        general = dict(self.suggestion, company_id="general", confidence="medium",
                       reason="The discussion is a personal learning session.")
        with self.assertRaises(WorkflowError):
            validate_result(general, self.source, self.company_list)
        self.company_list.append({"id": "general", "name": "General",
            "drive_folder_id": "PRIVATE_GENERAL_FOLDER",
            "description": "Personal, learning and broader planning meetings."})
        atomic_json(self.companies, self.company_list)
        self.invoke(self.answer(general))
        self.assertEqual(json.loads(self.output.read_text()), general)
        with self.assertRaises(WorkflowError):
            validate_result(dict(general, evidence=[]), self.source, self.company_list)
        abstention = dict(general, company_id=None, confidence="low", evidence=[], ambiguous=True,
                          reason="The transcript does not establish the meeting purpose.")
        validate_result(abstention, self.source, self.company_list)

    def test_invalid_input_and_configuration_do_not_start_harness(self):
        executable = self.fake("raise AssertionError('must not run')\n")
        for companies in ([], [self.company_list[0], self.company_list[0]],
                          [dict(self.company_list[0], description=42)],
                          [dict(self.company_list[0], aliases="Ackme")],
                          [dict(self.company_list[0], aliases=[" "])]):
            atomic_json(self.companies, companies)
            with self.assertRaises(WorkflowError):
                self.invoke(executable)
        atomic_json(self.companies, self.company_list)
        atomic_json(self.transcript, {"segments": []})
        with self.assertRaisesRegex(WorkflowError, "text segments"):
            self.invoke(executable)
        self.assertFalse(self.output.exists())

    def test_cli_errors_do_not_change_source_or_publish(self):
        original = self.transcript.read_bytes()
        with self.assertRaisesRegex(WorkflowError, "not installed"):
            self.invoke(str(self.root / "missing-codex"))
        with self.assertRaisesRegex(WorkflowError, "status 7"):
            self.invoke(self.fake("sys.stdin.read()\nsys.exit(7)\n"))
        with self.assertRaises(WorkflowError):
            self.invoke(self.fake("sys.stdin.read()\nPath('answer.json').write_text('not json')\n"))
        self.assertFalse(self.output.exists())
        self.assertEqual(self.transcript.read_bytes(), original)

    def test_existing_and_concurrent_output_are_preserved(self):
        self.output.write_text("existing revision")
        with self.assertRaisesRegex(WorkflowError, "already exists"):
            self.invoke(self.answer(self.suggestion))
        self.assertEqual(self.output.read_text(), "existing revision")
        self.output.unlink()
        executable = self.fake("sys.stdin.read()\nPath(" + repr(str(self.output)) + ")"
            ".write_text('concurrent revision')\nPath('answer.json').write_text("
            + repr(json.dumps(self.suggestion)) + ")\n")
        with self.assertRaisesRegex(WorkflowError, "already exists"):
            self.invoke(executable)
        self.assertEqual(self.output.read_text(), "concurrent revision")

    def test_timeout_stops_child_process_group(self):
        marker = self.root / "survived"
        executable = self.fake("import subprocess\nsys.stdin.read()\nsubprocess.Popen([sys.executable,'-c',"
            + repr("import time; from pathlib import Path; time.sleep(0.8); Path(" + repr(str(marker)) + ").touch()")
            + "])\ntime.sleep(10)\n")
        with self.assertRaisesRegex(WorkflowError, "timed out"):
            self.invoke(executable, timeout=0.2)
        time.sleep(1)
        self.assertFalse(marker.exists())
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
