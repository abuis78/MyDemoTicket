"""Routing tests for soar-playbooks/mydemoticket_es_mirror.py with a fake phantom.rules module."""

import importlib.util
import os
import sys
import types
import unittest

import support

PLAYBOOK = os.path.join(support.PLAYBOOK_DIR, "mydemoticket_es_mirror.py")
FIELDS = ("label", "number", "state", "assigned_to", "comment", "comment_author", "comment_type", "actor",
          "es_finding_id", "es_investigation_id", "source", "event", "artifact_id")


class FakeRules(types.ModuleType):
    def __init__(self):
        super().__init__("phantom.rules")
        self.rows, self.acts, self.notes = [], [], []

    def playbook_block(self):
        return lambda func: func

    def debug(self, *args, **kwargs):
        pass

    def collect2(self, container, datapath, scope=None):
        assert len(datapath) == len(FIELDS)
        return list(self.rows)

    def act(self, action, parameters=None, assets=None, name=None, callback=None):
        self.acts.append({"action": action, "parameters": parameters, "assets": assets, "name": name,
                          "callback": callback})

    def add_note(self, **kwargs):
        self.notes.append(kwargs)


def row(**values):
    base = {"label": "mydemoticket_event", "number": "TKT0001", "state": "in_progress", "assigned_to": "",
            "comment": "", "comment_author": None, "comment_type": None, "actor": None, "es_finding_id": "",
            "es_investigation_id": None, "source": "web", "event": "ticket.updated", "artifact_id": 7}
    base.update(values)
    return tuple(base[f] for f in FIELDS)


class TestEsMirrorPlaybook(unittest.TestCase):
    def setUp(self):
        import phantom
        self.rules = FakeRules()
        self._saved = (sys.modules.get("phantom.rules"), getattr(phantom, "rules", None))
        sys.modules["phantom.rules"] = self.rules
        phantom.rules = self.rules
        spec = importlib.util.spec_from_file_location("es_mirror_under_test", PLAYBOOK)
        self.pb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.pb)

    def tearDown(self):
        import phantom
        saved_module, saved_attr = self._saved
        if saved_module is None:
            sys.modules.pop("phantom.rules", None)
        else:
            sys.modules["phantom.rules"] = saved_module
        if saved_attr is None:
            delattr(phantom, "rules")
        else:
            phantom.rules = saved_attr

    def run_with(self, *rows):
        self.rules.rows = list(rows)
        self.pb.on_start({"id": 1})
        return {a["action"]: a for a in self.rules.acts}

    def test_comment_on_finding_adds_note_without_status_update(self):
        acts = self.run_with(row(event="ticket.commented", es_finding_id="f1@@notable@@time1", comment="Hello",
                                 comment_author="alice", comment_type="work_note"))
        self.assertEqual(sorted(acts), ["add finding or investigation note"])
        note = acts["add finding or investigation note"]
        self.assertEqual(note["assets"], ["builtin_mc_connector"])
        self.assertEqual(note["parameters"], [{"id": "f1@@notable@@time1",
                                               "content": "MyDemoTicket TKT0001 – work note/alice: Hello",
                                               "context": {"artifact_id": 7}}])
        self.assertIs(note["callback"], self.pb.add_es_note_done)

    def test_note_author_falls_back_to_actor_and_type_is_optional(self):
        acts = self.run_with(row(event="ticket.commented", es_finding_id="f1", comment="Hi", actor="bob"))
        content = acts["add finding or investigation note"]["parameters"][0]["content"]
        self.assertEqual(content, "MyDemoTicket TKT0001 – bob: Hi")

    def test_state_change_on_finding_uses_update_event_without_comment(self):
        acts = self.run_with(row(event="ticket.state_changed", state="resolved", es_finding_id="f1",
                                 assigned_to="analyst1"))
        self.assertEqual(sorted(acts), ["update event"])
        update = acts["update event"]
        self.assertEqual(update["assets"], ["splunk"])
        self.assertEqual(update["parameters"], [{"event_ids": "f1", "integer_status": "4", "owner": "analyst1",
                                                 "context": {"artifact_id": 7}}])
        self.assertIs(update["callback"], self.pb.update_es_event_done)

    def test_update_without_state_change_does_nothing_in_es(self):
        self.assertEqual(self.run_with(row(event="ticket.updated", es_finding_id="f1")), {})
        self.assertEqual(self.rules.notes, [])

    def test_investigation_gets_notes_and_close_status(self):
        acts = self.run_with(
            row(event="ticket.commented", es_finding_id="f1", es_investigation_id="inv-guid", comment="Note",
                comment_author="carol", artifact_id=8),
            row(event="ticket.state_changed", state="closed", es_finding_id="f1", es_investigation_id="inv-guid",
                artifact_id=9))
        self.assertEqual(sorted(acts), ["add finding or investigation note", "update finding or investigation"])
        self.assertEqual(acts["add finding or investigation note"]["parameters"][0]["id"], "inv-guid")
        update = acts["update finding or investigation"]
        self.assertEqual(update["assets"], ["builtin_mc_connector"])
        self.assertEqual(update["parameters"], [{"id": "inv-guid", "status": "5", "context": {"artifact_id": 9}}])

    def test_investigation_in_progress_does_not_update_status(self):
        self.assertEqual(self.run_with(row(event="ticket.state_changed", state="in_progress",
                                           es_investigation_id="inv-guid")), {})

    def test_soar_source_is_ignored(self):
        self.assertEqual(self.run_with(row(event="ticket.commented", source="SOAR", es_finding_id="f1",
                                           comment="echo")), {})
        self.assertEqual(self.rules.notes, [])

    def test_without_es_ids_adds_container_note(self):
        self.assertEqual(self.run_with(row(event="ticket.commented", comment="x|y")), {})
        self.assertEqual(len(self.rules.notes), 1)
        self.assertIn("x/y", self.rules.notes[0]["content"])

    def test_failed_note_callback_adds_container_note(self):
        self.pb.add_es_note_done(success=False, container={"id": 1})
        self.assertEqual(self.rules.notes[0]["title"], "MyDemoTicket: ES note failed")
        self.pb.add_es_note_done(success=True, container={"id": 1})
        self.assertEqual(len(self.rules.notes), 1)


if __name__ == "__main__":
    unittest.main()
