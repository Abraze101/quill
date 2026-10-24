"""Hybrid Codex transport tests. All tools and servers are synthetic."""
import copy
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quill_workflow.codex_drive_transport import (
    CONNECTOR, CodexDriveTransport, DriveOperationError, DriveProtocolError,
    DriveTimeoutError, DriveTransportError, _native_result, command, invoke, validate_arguments,
)
from quill_workflow.codex_file_upload import _UploadEvents, request, run_upload, upload_command


class CodexDriveTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.arguments = {"fileId": "folder123", "fields": "id,name,mimeType"}
        self.native = {"id": "folder123", "title": "Meetings",
                       "mime_type": "application/vnd.google-apps.folder",
                       "source_visibility_status": "access_not_verified"}
        self.capture = self.root / "requests.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def fake(self, native=None, *, startup="", call="", before="", thread=None):
        native = self.native if native is None else native
        setup = {"thread": {"id": "ephemeral-test"}} if thread is None else thread
        response = {"content": [{"type": "text", "text": "Action completed."}],
                    "structuredContent": native, "isError": False}
        code = "#!" + sys.executable + "\nimport json, os, sys, time\nfrom pathlib import Path\n"
        code += "def send(value):\n    print(json.dumps(value), flush=True)\n"
        code += startup + "\n"
        code += "for line in sys.stdin:\n"
        code += "    request = json.loads(line)\n"
        code += "    with Path(" + repr(str(self.capture)) + ").open('a') as capture:\n"
        code += "        capture.write(json.dumps(request) + '\\n')\n"
        code += textwrap.indent(before + "\n", "    ")
        code += "    method = request['method']\n"
        code += "    if method == 'initialize':\n        send({'id': 1, 'result': {'userAgent': 'fixture'}})\n"
        code += "    elif method == 'initialized':\n        continue\n"
        code += "    elif method == 'thread/start':\n        send({'id': 2, 'result': " + repr(setup) + "})\n"
        code += "    elif method == 'mcpServer/tool/call':\n"
        code += textwrap.indent(call or "send({'id': 3, 'result': " + repr(response) + "})", "        ") + "\n"
        code += "    else:\n        raise AssertionError('Unexpected request: ' + method)\n"
        path = self.root / "fake codex"
        path.write_text(code)
        path.chmod(0o700)
        return str(path)

    def requests(self):
        return [json.loads(line) for line in self.capture.read_text().splitlines()]

    def run_read(self, **fake_options):
        return invoke("get_file_metadata", self.arguments, executable=self.fake(**fake_options))

    def test_actual_normalized_mcp_response_and_exact_rpc_no_model_turn(self):
        self.assertEqual(self.run_read(), self.native)
        requests = self.requests()
        self.assertEqual([request["method"] for request in requests],
                         ["initialize", "initialized", "thread/start", "mcpServer/tool/call"])
        setup = requests[2]["params"]
        self.assertIs(setup["ephemeral"], True)
        self.assertEqual(setup["runtimeWorkspaceRoots"], [])
        self.assertEqual(setup["environments"], [])
        self.assertEqual(setup["sandbox"], "read-only")
        self.assertEqual(setup["approvalPolicy"], "never")
        self.assertEqual(requests[3], {"id": 3, "method": "mcpServer/tool/call", "params": {
            "threadId": "ephemeral-test", "server": "codex_apps", "tool": "google_drive.get_file_metadata",
            "arguments": self.arguments}})
        self.assertFalse(Path(setup["cwd"]).exists())
        self.assertNotIn("turn/start", [request["method"] for request in requests])

    def test_saved_auth_private_process_only_config_and_no_model_override(self):
        capture = self.root / "process.json"
        startup = "Path(" + repr(str(capture)) + ").write_text(json.dumps({'argv':sys.argv, 'cwd':os.getcwd(), 'env':dict(os.environ), 'mode':Path.cwd().stat().st_mode & 511}))"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "not-a-key", "ANTHROPIC_API_KEY": "not-a-key",
                                     "CODEX_THREAD_ID": "do-not-inherit", "NODE_OPTIONS": "bad"}):
            result = invoke("get_file_metadata", self.arguments, executable=self.fake(startup=startup),
                            model="compatibility-value-never-used")
        self.assertEqual(result, self.native)
        captured = json.loads(capture.read_text())
        argv = captured["argv"]
        self.assertEqual(argv[1:3], ["app-server", "--stdio"])
        self.assertNotIn("exec", argv)
        self.assertNotIn("--model", argv)
        self.assertNotIn("compatibility-value-never-used", argv)
        self.assertEqual(captured["mode"], 0o700)
        self.assertFalse(Path(captured["cwd"]).exists())
        for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CODEX_THREAD_ID", "NODE_OPTIONS"):
            self.assertNotIn(key, captured["env"])
        for setting in ('forced_login_method="chatgpt"', 'approval_policy="never"',
                        'web_search="disabled"', "apps={}", "apps._default.enabled=false", "mcp_servers={}",
                        "skills.include_instructions=false", f"apps.{CONNECTOR}.default_tools_enabled=false",
                        f"apps.{CONNECTOR}.tools.get_file_metadata.enabled=true",
                        f'apps.{CONNECTOR}.tools.get_file_metadata.approval_mode="auto"'):
            self.assertIn(setting, argv)
        self.assertLess(argv.index("apps={}"), argv.index("apps._default.enabled=false"))
        for feature in ("shell_tool", "unified_exec", "hooks", "browser_use", "computer_use",
                        "multi_agent", "memories", "code_mode", "view_image"):
            self.assertIn("features." + feature + "=false", argv)
        self.assertFalse(any(arg.startswith("features.code_mode_host=") for arg in argv))
        self.assertEqual(len([arg for arg in argv if ".tools." in arg]), 2)

    def test_write_approval_is_process_scoped_to_only_requested_method(self):
        args = command("/path/codex", self.root, "create_folder", model="ignored")
        self.assertIn(f'apps.{CONNECTOR}.tools.create_folder.approval_mode="approve"', args)
        self.assertIn(f'apps.{CONNECTOR}.default_tools_approval_mode="auto"', args)
        self.assertNotIn('apps._default.default_tools_approval_mode="approve"', args)
        self.assertEqual([arg for arg in args if ".tools." in arg], [
            f"apps.{CONNECTOR}.tools.create_folder.enabled=true",
            f'apps.{CONNECTOR}.tools.create_folder.approval_mode="approve"'])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_all_supported_tools_return_native_structured_content(self):
        local = self.root / "notes.md"
        local.write_text("Synthetic private notes")
        cases = [
            ("search", {"best_effort_fetch": False, "special_filter_query_str": "'folder123' in parents", "topn": 10},
             {"results": [], "next_page_token": None}),
            ("list_folder", {"url": "https://drive.google.com/drive/folders/folder123", "top_k": 100},
             {"files": [{"id": "notes123", "title": "notes.json", "mime_type": "application/json"}]}),
            ("create_folder", {"name": "Meeting", "parent_folder": "folder123"},
             {"success": True, "id": "meeting123", "parent_id": "folder123"}),
        ]
        for tool, arguments, native in cases:
            with self.subTest(tool=tool):
                self.assertEqual(invoke(tool, arguments, executable=self.fake({"result": native})), native)
                call = self.requests()[-1]
                self.assertEqual(call["params"]["tool"], "google_drive." + tool)
                self.assertEqual(call["params"]["arguments"], arguments)
        self.assertEqual(local.read_text(), "Synthetic private notes")

    def test_list_folder_has_one_read_only_method_and_strict_url_limit_and_result(self):
        args = {"url": "https://drive.google.com/drive/folders/private_123-abc", "top_k": 100}
        self.assertEqual(invoke("list_folder", args, executable=self.fake({"files": []})), {"files": []})
        self.assertEqual(self.requests()[-1]["params"]["tool"], "google_drive.list_folder")
        self.assertEqual(self.requests()[-1]["params"]["arguments"], args)
        argv = command("codex", self.root, "list_folder")
        self.assertEqual([arg for arg in argv if ".tools." in arg], [
            f"apps.{CONNECTOR}.tools.list_folder.enabled=true",
            f'apps.{CONNECTOR}.tools.list_folder.approval_mode="auto"'])
        bad_urls = ["http://drive.google.com/drive/folders/private123",
                    "https://drive.google.com.evil.test/drive/folders/private123",
                    "https://drive.google.com/drive/folders/root",
                    "https://drive.google.com/drive/folders/private123?usp=sharing",
                    "https://drive.google.com/drive/folders/private123/",
                    "https://drive.google.com/drive/folders/../private123",
                    "https://drive.google.com/drive/folders/a%2Fb", "private123"]
        for bad in ([dict(args, url=url) for url in bad_urls]
                    + [dict(args, top_k=value) for value in (True, 0, 101, 1.5)]
                    + [{"url": args["url"]}, dict(args, page_token="unexpected")]):
            with self.subTest(arguments=bad):
                with self.assertRaises(DriveProtocolError):
                    validate_arguments("list_folder", bad)
        for result in ({"results": []}, {"files": {}}, {"files": ["unvalidated"]}):
            with self.subTest(result=result):
                with self.assertRaisesRegex(DriveProtocolError, "files list"):
                    invoke("list_folder", args, executable=self.fake(result))

    def test_native_document_readback_uses_exact_read_method_and_rejects_urls(self):
        native = {"documentId": "document123", "tabs": [{"tabId": "t.0", "body": {"content": []}}]}
        args = {"document_id": "document123"}
        self.assertEqual(invoke("get_document", args, executable=self.fake(native)), native)
        self.assertEqual(self.requests()[-1]["params"]["tool"], "google_drive.get_document")
        for bad in ({"document_id": "https://docs.google.com/document/d/document123/edit"},
                    {"document_id": "document123", "fields": "body"}, {"document_url": "document123"}):
            with self.subTest(arguments=bad), self.assertRaises(DriveProtocolError):
                validate_arguments("get_document", bad)

    def test_native_template_copy_and_revision_guarded_update_use_no_model(self):
        copy_args = {"url": "https://docs.google.com/document/d/template_123456/edit",
                     "new_title": "Planning - Notes", "parent_folder": "notes_folder123"}
        native = {"success": True, "id": "copied_document123", "url": "https://docs.google.com/document/d/copied_document123/edit"}
        self.assertEqual(invoke("copy_file", copy_args, executable=self.fake(native)), native)
        self.assertEqual(self.requests()[-1]["params"]["arguments"], copy_args)
        edit_args = {"document_id": "copied_document123", "requests": [{"replaceAllText": {
            "containsText": {"text": "{{QUILL_TITLE}}", "matchCase": True}, "replaceText": "Planning"}}],
            "write_control": {"requiredRevisionId": "before-revision"}}
        updated = {"documentId": "copied_document123", "replies": [{"replaceAllText": {"occurrencesChanged": 1}}]}
        self.assertEqual(invoke("batch_update_document", edit_args, executable=self.fake(updated)), updated)
        self.assertEqual(self.requests()[-1]["params"]["tool"], "google_drive.batch_update_document")
        self.assertNotIn("turn/start", [row["method"] for row in self.requests()])
        self.assertEqual(self.requests()[-1]["params"]["arguments"], edit_args)
        long_args = copy.deepcopy(edit_args)
        long_args["requests"][0]["replaceAllText"]["replaceText"] = "Transcript " * 15000
        self.assertEqual(validate_arguments("batch_update_document", long_args), long_args)

    def test_native_template_transport_rejects_unscoped_mutations(self):
        args = {"document_id": "copied_document123", "requests": [{"insertText": {
            "location": {"index": 1}, "text": "Meeting"}}], "write_control": {"requiredRevisionId": "revision"}}
        for bad in (dict(args, requests=[]), dict(args, requests=[{"deleteDocumentTab": {"tabId": "t.0"}}]),
                    dict(args, write_control={"targetRevisionId": "merge"}), dict(args, write_control={}),
                    {key: value for key, value in args.items() if key != "write_control"},
                    dict(args, image_uris="/unrequested.png")):
            with self.subTest(arguments=bad), self.assertRaises(DriveProtocolError):
                validate_arguments("batch_update_document", bad)
        for url in ("https://docs.google.com.evil.test/document/d/template123/edit",
                    "https://docs.google.com/document/d/template123/edit?x=1", "template123"):
            with self.subTest(url=url), self.assertRaises(DriveProtocolError):
                validate_arguments("copy_file", {"url": url, "new_title": "Notes", "parent_folder": "parent123"})
        for native in ({"documentId": "wrong_document", "replies": []},
                       {"documentId": "copied_document123"}, {"success": True, "id": "copied_document123"}):
            with self.subTest(native=native), self.assertRaises(DriveProtocolError):
                _native_result({"structuredContent": native}, "batch_update_document", args)

    def test_malicious_strings_are_literal_json_arguments_never_programs(self):
        name = 'Notes "); await tools.share_file({}); // ` ${value} \\n'
        args = {"name": name, "parent_folder": "folder123"}
        invoke("create_folder", args, executable=self.fake({"success": True, "id": "new-folder"}))
        self.assertEqual(self.requests()[-1]["params"]["arguments"], args)
        for request in self.requests():
            self.assertNotIn("program", request)
            self.assertNotIn("prompt", request)
        for method in ("share_file", "create_folder); injected(); //", "__proto__", [], None):
            with self.subTest(method=method):
                with self.assertRaises(DriveProtocolError):
                    validate_arguments(method, args)

    def test_approval_and_elicitation_requests_are_not_answered(self):
        for method in ("item/tool/requestUserInput", "tool/requestUserInput", "mcpServer/elicitation/request",
                       "item/permissions/requestApproval"):
            with self.subTest(method=method):
                if self.capture.exists(): self.capture.unlink()
                call = "send({'id': 50, 'method': " + repr(method) + ", 'params': {'question':'Approve?'}})"
                with self.assertRaisesRegex(DriveOperationError, "approval or confirmation") as caught:
                    self.run_read(call=call)
                self.assertFalse(caught.exception.outcome_unknown)
                self.assertEqual(len(self.requests()), 4)
                self.assertFalse(any(request.get("id") == 50 for request in self.requests()))

    def test_unexpected_server_requests_and_tool_notifications_fail_closed(self):
        for message in ({"id": 9, "method": "item/tool/call", "params": {"tool": "shell"}},
                        {"method": "turn/started", "params": {}},
                        {"method": "item/started", "params": {"item": {"type": "mcpToolCall", "tool": "share_file"}}}):
            with self.subTest(message=message):
                with self.assertRaises(DriveProtocolError):
                    self.run_read(call="send(" + repr(message) + ")")

    def test_duplicate_or_wrong_rpc_ids_fail_closed(self):
        result = {"content": [], "structuredContent": self.native, "isError": False}
        for call in ("send({'id': 2, 'result': " + repr(result) + "})",
                     "send({'id': '3', 'result': " + repr(result) + "})",
                     "sys.stdout.write(json.dumps({'id':3,'result':" + repr(result) + "}) + '\\n' + json.dumps({'id':3,'result':" + repr(result) + "}) + '\\n'); sys.stdout.flush()"):
            with self.subTest(call=call):
                with self.assertRaises(DriveProtocolError):
                    self.run_read(call=call)

    def test_connector_failure_wrong_identity_and_prose_are_not_results(self):
        cases = [({"isError": True, "structuredContent": self.native}, DriveOperationError),
                 ({"is_error": True, "structuredContent": self.native}, DriveOperationError),
                 ({"isError": False, "is_error": True, "structuredContent": self.native}, DriveOperationError),
                 ({"isError": "false", "structuredContent": self.native}, DriveProtocolError),
                 ({"content": [{"type": "text", "text": '{"id":"folder123"}'}]}, DriveProtocolError),
                 ({"structuredContent": dict(self.native, id="wrong")}, DriveProtocolError)]
        for result, error in cases:
            with self.subTest(result=result):
                with self.assertRaises(error):
                    self.run_read(call="send({'id':3, 'result':" + repr(result) + "})")
        with self.assertRaisesRegex(DriveOperationError, "requires approval"):
            self.run_read(call="send({'id':3,'error':{'code':-1,'message':'MCP tool call requires approval, but approval policy is never'}})")

    def test_read_only_root_alias_resolves_concrete_authenticated_id(self):
        arguments = dict(self.arguments, fileId="root")
        native = dict(self.native, id="my_drive_root_123", title="My Drive", parent_ids=None)
        self.assertEqual(invoke("get_file_metadata", arguments, executable=self.fake(native=native)), native)
        self.assertEqual(self.requests()[-1]["params"]["arguments"]["fileId"], "root")
        for identity in (None, "", "root", "../not-a-root", "x" * 201):
            with self.subTest(identity=identity), self.assertRaises(DriveProtocolError):
                _native_result({"structuredContent": dict(native, id=identity)}, "get_file_metadata", arguments)

    def test_root_alias_remains_rejected_for_writes_and_document_reads(self):
        for tool, args in (("create_folder", {"name": "Meeting", "parent_folder": "root"}),
                           ("copy_file", {"url": "https://docs.google.com/document/d/document_123/edit",
                                          "new_title": "Notes", "parent_folder": "root"}),
                           ("get_document", {"document_id": "root"})):
            with self.subTest(tool=tool), self.assertRaises(DriveProtocolError):
                validate_arguments(tool, args)

    def test_write_failures_after_dispatch_are_unknown_and_never_retried(self):
        args = {"name": "Meeting", "parent_folder": "folder123"}
        for call in ("sys.exit(9)", "send({'id':3,'error':{'code':-1,'message':'Connection lost'}})",
                     "send({'id':3,'result':{'structuredContent':{'success':False}}})"):
            with self.subTest(call=call):
                if self.capture.exists(): self.capture.unlink()
                with self.assertRaises(DriveTransportError) as caught:
                    invoke("create_folder", args, executable=self.fake(call=call))
                self.assertTrue(caught.exception.outcome_unknown)
                self.assertEqual(sum(request["method"] == "mcpServer/tool/call" for request in self.requests()), 1)

    def test_startup_failure_is_known_not_dispatched_even_for_write(self):
        args = {"name": "Meeting", "parent_folder": "folder123"}
        with self.assertRaises(DriveOperationError) as caught:
            invoke("create_folder", args, executable=self.fake(startup="sys.exit(4)"))
        self.assertFalse(caught.exception.outcome_unknown)
        self.assertFalse(self.capture.exists())

        with self.assertRaises(DriveProtocolError) as caught:
            invoke("create_folder", args, executable=self.fake(thread={"thread": {"id": "persistent", "ephemeral": False}}))
        self.assertFalse(caught.exception.outcome_unknown)

    def test_output_bounds_duplicate_json_and_malformed_json_fail_closed(self):
        for output in ("x" * 10000, '{"id":3,"id":4}\n', '{"id":3,"value":NaN}\n', "not-json\n"):
            with self.subTest(output=output[:30]):
                with self.assertRaises(DriveProtocolError):
                    invoke("get_file_metadata", self.arguments,
                           executable=self.fake(call="sys.stdout.write(" + repr(output) + "); sys.stdout.flush()"),
                           max_output_bytes=1024)
        with self.assertRaisesRegex(DriveProtocolError, "size limit"):
            self.run_read(call="sys.stderr.write('x' * 140000); sys.stderr.flush()")

    def test_timeout_kills_descendants_and_preserves_unknown_write_outcome(self):
        marker = self.root / "child-survived"
        child = "import time; from pathlib import Path; time.sleep(0.8); Path(" + repr(str(marker)) + ").touch()"
        call = "import subprocess\nsubprocess.Popen([sys.executable, '-c', " + repr(child) + "])\ntime.sleep(10)"
        with self.assertRaises(DriveTimeoutError) as caught:
            invoke("create_folder", {"name": "Meeting", "parent_folder": "folder123"},
                   executable=self.fake(call=call), timeout=0.2)
        self.assertTrue(caught.exception.outcome_unknown)
        time.sleep(1)
        self.assertFalse(marker.exists())

    def test_delete_cancels_dispatched_owned_operation_and_preserves_uncertainty(self):
        dispatched = self.root / "dispatched"
        pidfile = self.root / "owned-pid"
        call = "Path(" + repr(str(dispatched)) + ").touch()\nPath(" + repr(str(pidfile)) + ").write_text(str(os.getpid()))\ntime.sleep(20)"
        transport = CodexDriveTransport(executable=self.fake(call=call), cancelled=dispatched.exists)
        with self.assertRaises(DriveOperationError) as caught:
            transport.invoke("create_folder", {"name": "Meeting", "parent_folder": "folder123"})
        self.assertTrue(caught.exception.outcome_unknown)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    def test_delete_before_dispatch_does_not_launch_or_claim_unknown_remote_write(self):
        transport = CodexDriveTransport(executable=self.fake(), cancelled=lambda: True)
        with self.assertRaises(DriveOperationError) as caught:
            transport.invoke("create_folder", {"name": "Meeting", "parent_folder": "folder123"})
        self.assertFalse(caught.exception.outcome_unknown)
        self.assertFalse(self.capture.exists())

    def test_signal_handler_is_restored_after_success_or_error(self):
        previous = signal.getsignal(signal.SIGTERM)
        self.run_read()
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)
        with self.assertRaises(DriveOperationError):
            self.run_read(call="sys.exit(5)")
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_input_validation_does_not_launch_server(self):
        executable = self.fake()
        cases = [("share_file", {}), ("get_file_metadata", {"fileId": "folder123"}),
                 ("get_file_metadata", dict(self.arguments, fileId="https://drive.google.com/folder")),
                 ("search", {"best_effort_fetch": True, "topn": 10, "special_filter_query_str": "x"}),
                 ("search", {"best_effort_fetch": False, "topn": True, "special_filter_query_str": "x"}),
                 ("create_folder", {"name": "Meeting", "parent_folder": "root"}),
                 ("upload_file", {"file_uri": "relative.txt", "file_name": "notes.txt",
                                  "mime_type": "text/plain", "parent_folder_id": "folder123"})]
        for tool, arguments in cases:
            with self.subTest(tool=tool):
                with self.assertRaises(DriveProtocolError):
                    invoke(tool, arguments, executable=executable)
        self.assertFalse(self.capture.exists())
        for options in ({"timeout": 0}, {"timeout": float("nan")}, {"max_output_bytes": 10}):
            with self.assertRaises(DriveProtocolError):
                CodexDriveTransport(**options)
        with self.assertRaises(DriveTransportError) as caught:
            invoke("create_folder", {"name": "Meeting", "parent_folder": "folder123"},
                   executable=str(self.root / "missing"))
        self.assertFalse(caught.exception.outcome_unknown)


class CodexFileUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.local = self.root / "notes.txt"
        self.local.write_text("Synthetic content; never included in the model prompt.")
        self.args = {"file_uri": str(self.local), "file_name": 'notes "); injected(); // `${x}`.txt',
                     "mime_type": "text/plain", "parent_folder_id": "private123"}
        self.capture = self.root / "capture.json"
        self.result = {"success": True, "id": "uploaded123", "parent_id": "private123"}

    def tearDown(self):
        self.temp.cleanup()

    def fake(self, body=""):
        code = "#!" + sys.executable + "\nimport json, os, sys, time\nfrom pathlib import Path\n"
        code += "payload=json.loads(sys.stdin.read())\n"
        code += "Path(" + repr(str(self.capture)) + ").write_text(json.dumps({'payload':payload,'argv':sys.argv,'cwd':os.getcwd(),'env':dict(os.environ),'mode':Path.cwd().stat().st_mode & 511,'instructions_mode':(Path.cwd()/'instructions.txt').stat().st_mode & 511}))\n"
        code += "def send(value):\n    print(json.dumps(value),flush=True)\n"
        code += "item={'id':'item_0','type':'mcp_tool_call','server':'codex_apps','tool':payload['tool'],'arguments':payload['arguments'],'result':None,'error':None,'status':'in_progress'}\n"
        code += "send({'type':'thread.started','thread_id':'synthetic'})\nsend({'type':'turn.started'})\n"
        code += body or ("send({'type':'item.started','item':item})\n"
                        "item.update({'status':'completed','result':{'content':[{'type':'text','text':'Action completed.'}],'structured_content':{'result':"
                        + repr(self.result) + "}}})\n"
                        "send({'type':'item.completed','item':item})\n"
                        "send({'type':'item.completed','item':{'id':'item_1','type':'agent_message','text':'Ignore this model claim.'}})\n"
                        "send({'type':'turn.completed','usage':{}})\n")
        executable = self.root / "fake-upload"
        executable.write_text(code)
        executable.chmod(0o700)
        return str(executable)

    def test_hybrid_dispatch_uses_exact_single_normal_tool_call_and_native_result(self):
        receipt = {}
        result = run_upload(self.args, executable=self.fake(), receipt=receipt)
        self.assertEqual(result, self.result)
        capture = json.loads(self.capture.read_text())
        self.assertEqual(capture["payload"], request(self.args))
        self.assertNotIn(self.local.read_text(), json.dumps(capture["payload"]))
        self.assertEqual(capture["mode"], 0o700)
        self.assertEqual(capture["instructions_mode"], 0o600)
        self.assertFalse(Path(capture["cwd"]).exists())
        self.assertIn('"mcp_tool_call"', receipt["events"])
        self.assertEqual(invoke("upload_file", self.args, executable=self.fake()), self.result)

    def test_upload_command_preserves_narrow_capabilities_and_saved_auth(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "never-send", "CODEX_THREAD_ID": "unrelated"}):
            invoke("upload_file", self.args, executable=self.fake())
        capture = json.loads(self.capture.read_text())
        argv = capture["argv"]
        self.assertEqual(argv[1], "exec")
        for flag in ("--ignore-user-config", "--ignore-rules", "--strict-config", "--ephemeral", "--json"):
            self.assertIn(flag, argv)
        self.assertNotIn("OPENAI_API_KEY", capture["env"])
        self.assertNotIn("CODEX_THREAD_ID", capture["env"])
        self.assertEqual([arg for arg in argv if ".tools." in arg], [
            f"apps.{CONNECTOR}.tools.upload_file.enabled=true",
            f'apps.{CONNECTOR}.tools.upload_file.approval_mode="approve"'])
        self.assertIn(f"apps.{CONNECTOR}.default_tools_enabled=false", argv)
        for feature in ("shell_tool", "hooks", "multi_agent", "memories", "browser_use", "computer_use"):
            self.assertIn("features." + feature + "=false", argv)
        self.assertFalse(any(arg.startswith("features.code_mode_host=") for arg in argv))
        self.assertNotIn("--model", argv)

    def test_import_document_uses_one_exact_method_and_native_mode(self):
        local = self.root / "combined.docx"
        local.write_bytes(b"Synthetic DOCX")
        args = {"source_file": str(local), "title": 'Notes and transcript "); injected(); //',
                "parent_folder_id": "private123", "upload_mode": "native_google_docs"}
        self.result = {"success": True, "converted": True, "fileId": "document123",
                       "documentId": "document123", "mimeType": "application/vnd.google-apps.document",
                       "parentId": "private123", "title": args["title"],
                       "document_url": "https://docs.google.com/document/d/document123"}
        result = invoke("import_document", args, executable=self.fake())
        self.assertEqual(result, dict(self.result, id="document123",
                                     mime_type="application/vnd.google-apps.document", parent_id="private123"))
        capture = json.loads(self.capture.read_text())
        self.assertEqual(capture["payload"], request(args, "import_document"))
        self.assertEqual(capture["payload"]["tool"], "google_drive.import_document")
        self.assertEqual([arg for arg in capture["argv"] if ".tools." in arg], [
            f"apps.{CONNECTOR}.tools.import_document.enabled=true",
            f'apps.{CONNECTOR}.tools.import_document.approval_mode="approve"'])
        for bad in (dict(args, upload_mode="keep_source_file_type"),
                    dict(args, source_file=str(self.local)), dict(args, parent_folder_id="root"),
                    dict(args, source_file="relative.docx"), dict(args, arbitrary="extra")):
            with self.subTest(arguments=bad), self.assertRaises(DriveProtocolError):
                validate_arguments("import_document", bad)

    def test_import_result_requires_matching_native_conversion_identity_parent_and_title(self):
        args = {"title": "Combined meeting document", "parent_folder_id": "private123"}
        native = {"success": True, "converted": True, "fileId": "document123",
                  "documentId": "document123", "mimeType": "application/vnd.google-apps.document",
                  "parentId": "private123", "title": args["title"]}
        result = _native_result({"structuredContent": native}, "import_document", args)
        self.assertEqual(result["id"], native["fileId"])
        self.assertEqual(result["mime_type"], native["mimeType"])
        for changes in ({"success": False}, {"converted": False}, {"converted": "true"},
                        {"mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
                        {"fileId": None}, {"documentId": "different123"}, {"id": "different123"},
                        {"fileId": "https://docs.google.com/document/d/document123"},
                        {"mime_type": "text/plain"}, {"parentId": "different123"},
                        {"title": "Different title"}):
            with self.subTest(changes=changes), self.assertRaises(DriveTransportError):
                _native_result({"structuredContent": dict(native, **changes)}, "import_document", args)
        missing = dict(native)
        del missing["documentId"]
        with self.assertRaises(DriveProtocolError):
            _native_result({"structuredContent": missing}, "import_document", args)

    def test_import_rejects_a_raw_upload_or_second_write(self):
        local = self.root / "combined.docx"
        local.write_bytes(b"Synthetic DOCX")
        args = {"source_file": str(local), "title": "Notes and transcript",
                "parent_folder_id": "private123", "upload_mode": "native_google_docs"}
        for body in ("item['tool']='google_drive.upload_file'\nsend({'type':'item.started','item':item})\n",
                     "send({'type':'item.started','item':item})\nsend({'type':'item.started','item':item})\n"):
            with self.subTest(body=body), self.assertRaises(DriveProtocolError):
                invoke("import_document", args, executable=self.fake(body))

    def test_observed_runtime_upload_result_shape_and_original_arguments(self):
        # The live CLI preserved the original path in BOTH item events, although
        # the internal bridge transferred a file reference to the connector.
        native = dict(self.result, accepted_input="file_uri", file_name=self.args["file_name"],
                      title=self.args["file_name"], mime_type="text/plain")
        observed = dict(native, structuredContent=native)
        body = ("send({'type':'item.started','item':item})\n"
                "item.update({'status':'completed','result':{'content':[{'type':'text','text':'Action completed.'}],"
                "'structured_content':" + repr(observed) + "}})\n"
                "send({'type':'item.completed','item':item})\nsend({'type':'turn.completed','usage':{}})\n")
        self.assertEqual(invoke("upload_file", self.args, executable=self.fake(body)), observed)
        capture = json.loads(self.capture.read_text())
        self.assertEqual(capture["payload"]["arguments"], self.args)

    def test_literal_arguments_cannot_inject_javascript_into_fixed_program(self):
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node or not Path(node).is_file():
            self.skipTest("Node runtime unavailable")
        args = dict(self.args, file_name='\"); globalThis.injected=true; //\\\n` ${evil} \u2028 test')
        program = request(args)["program"]
        script = ("let count=0; const tools={mcp__codex_apps__google_drive_upload_file:async args=>{count++;return args}};"
                  "const text=value=>console.log(JSON.stringify(value));\n" + program
                  + "\nif(count!==1 || globalThis.injected) throw Error('Injected program');")
        completed = subprocess.run([node, "--input-type=module", "-"], input=script, text=True,
                                   capture_output=True, timeout=10, check=True)
        self.assertEqual(json.loads(completed.stdout), args)

    def test_changed_arguments_extra_tools_and_duplicate_writes_fail_closed(self):
        bodies = [
            "item['arguments']['parent_folder_id']='another-folder'\nsend({'type':'item.started','item':item})\n",
            "item['tool']='google_drive.share_file'\nsend({'type':'item.started','item':item})\n",
            "send({'type':'item.started','item':{'type':'command_execution','id':'shell','command':'ls'}})\n",
            "send({'type':'item.started','item':item})\nsend({'type':'item.started','item':item})\n",
            "send({'type':'item.completed','item':{'type':'agent_message','id':'message','text':'Upload succeeded'}})\nsend({'type':'turn.completed','usage':{}})\n",
        ]
        for body in bodies:
            with self.subTest(body=body):
                with self.assertRaises(DriveProtocolError) as caught:
                    invoke("upload_file", self.args, executable=self.fake(body))
                self.assertTrue(caught.exception.outcome_unknown)

    def test_startup_error_fixture_has_clear_operation_error(self):
        with self.assertRaisesRegex(DriveOperationError, "startup"):
            invoke("upload_file", self.args, executable=self.fake(
                "send({'type':'item.completed','item':{'type':'error','id':'item_0','message':'Code Mode is unavailable because code-mode host is disabled.'}})\n"))

    def test_failed_tool_and_unverified_text_results_never_succeed(self):
        for result, error, status in [(None, {"message": "Connection lost"}, "failed"),
                                      ({"content": [{"type":"text", "text":'{"success":true,"id":"fake"}'}]}, None, "completed"),
                                      ({"structured_content":{"success":True,"id":"uploaded123"},"is_error":True}, None, "completed"),
                                      ({"structured_content":{"success":True,"id":"uploaded123"},"isError":True}, None, "completed"),
                                      ({"structured_content":{"success":True,"id":"uploaded123"},"is_error":False,"isError":True}, None, "completed"),
                                      ({"structured_content":{"success":True,"id":"uploaded123"},"is_error":"false"}, None, "completed")]:
            body = ("send({'type':'item.started','item':item})\nitem.update(" + repr({"result":result,"error":error,"status":status})
                    + ")\nsend({'type':'item.completed','item':item})\n")
            with self.subTest(result=result):
                with self.assertRaises(DriveTransportError) as caught:
                    invoke("upload_file", self.args, executable=self.fake(body))
                self.assertTrue(caught.exception.outcome_unknown)

    def test_upload_output_limits_and_timeout_kill_entire_process_group(self):
        marker = self.root / "orphan"
        child = "import time;from pathlib import Path;time.sleep(.8);Path(" + repr(str(marker)) + ").touch()"
        body = "import subprocess\nsubprocess.Popen([sys.executable,'-c'," + repr(child) + "])\ntime.sleep(10)\n"
        with self.assertRaises(DriveTimeoutError) as caught:
            invoke("upload_file", self.args, executable=self.fake(body), timeout=0.2)
        self.assertTrue(caught.exception.outcome_unknown)
        time.sleep(1)
        self.assertFalse(marker.exists())
        for body in ("sys.stdout.write('x'*2000);sys.stdout.flush()\n", "sys.stderr.write('x'*140000);sys.stderr.flush()\n"):
            with self.assertRaisesRegex(DriveProtocolError, "size limit"):
                invoke("upload_file", self.args, executable=self.fake(body), max_output_bytes=1024)

    def test_delete_cancels_launched_upload_and_keeps_unknown_write_outcome(self):
        launched = self.root / "launched"
        body = "Path(" + repr(str(launched)) + ").touch()\ntime.sleep(20)\n"
        with self.assertRaises(DriveOperationError) as caught:
            run_upload(self.args, executable=self.fake(body), cancelled=launched.exists)
        self.assertTrue(caught.exception.outcome_unknown)

    def test_duplicate_completion_and_trailing_other_operation_are_rejected(self):
        parser = _UploadEvents(self.args)
        parser.consume(json.dumps({"type":"thread.started","thread_id":"test"}))
        parser.consume(json.dumps({"type":"turn.started"}))
        item = {"id":"first","type":"mcp_tool_call","server":"codex_apps","tool":"google_drive.upload_file",
                "arguments":self.args,"result":None,"error":None,"status":"in_progress"}
        parser.consume(json.dumps({"type":"item.started","item":item}))
        item.update({"status":"completed","result":{"structured_content":self.result}})
        parser.consume(json.dumps({"type":"item.completed","item":item}))
        with self.assertRaises(DriveProtocolError):
            parser.consume(json.dumps({"type":"item.completed","item":item}))
        parser.consume(json.dumps({"type":"turn.completed","usage":{}}))
        with self.assertRaises(DriveProtocolError):
            parser.consume(json.dumps({"type":"item.started","item":dict(item, id="second")}))


if __name__ == "__main__":
    unittest.main()
