"""Finding the tests a fix targets, and narrowing fail_to_pass to them."""

from bugfix.models import Issue, PullRequest, Record, Verification
from bugfix.targets import narrow, touched_tests
from bugfix.verify import decide, refine

NEW_TEST = """\
diff --git a/tests/test_cli.py b/tests/test_cli.py
index 1111111..2222222 100644
--- a/tests/test_cli.py
+++ b/tests/test_cli.py
@@ -10,6 +10,10 @@ def test_old():
     assert True


+def test_new_option():
+    assert run("--new") == 0
+
+
 def test_other():
     assert True
"""

EDITED_METHOD = """\
diff --git a/testing/test_junitxml.py b/testing/test_junitxml.py
--- a/testing/test_junitxml.py
+++ b/testing/test_junitxml.py
@@ -40,7 +40,7 @@ class TestPython:
     def test_summing(self):
         x = 1
-        assert x == 2
+        assert x == 1

     def test_untouched(self):
         pass
"""

INSIDE_FROM_HEADER = """\
diff --git a/tests/test_app.py b/tests/test_app.py
--- a/tests/test_app.py
+++ b/tests/test_app.py
@@ -88,4 +88,5 @@ def test_long_one(app):
     client = app.test_client()
     rv = client.get("/")
+    assert rv.status_code == 200
     assert rv.data
"""

NEW_PARAM = """\
diff --git a/tests/test_parse.py b/tests/test_parse.py
--- a/tests/test_parse.py
+++ b/tests/test_parse.py
@@ -5,6 +5,7 @@ import pytest
 @pytest.mark.parametrize("value", [
     "a",
     "b",
+    "",
 ])
 def test_parse(value):
     assert parse(value)
"""

FIXTURE_ONLY = """\
diff --git a/tests/conftest.py b/tests/conftest.py
--- a/tests/conftest.py
+++ b/tests/conftest.py
@@ -1,3 +1,4 @@
 import pytest
+from app import new_helper

 @pytest.fixture
"""

ASYNC_TEST = """\
diff --git a/tests/test_async.py b/tests/test_async.py
--- a/tests/test_async.py
+++ b/tests/test_async.py
@@ -1,2 +1,5 @@
 import asyncio
+
+async def test_waits():
+    await asyncio.sleep(0)
"""


def test_finds_a_new_test_not_the_one_named_in_the_hunk_header():
    assert touched_tests(NEW_TEST) == {("tests.test_cli", "test_new_option")}


def test_finds_an_edited_method_inside_a_class():
    assert touched_tests(EDITED_METHOD) == {("testing.test_junitxml", "test_summing")}


def test_uses_the_hunk_header_when_the_def_line_is_out_of_view():
    assert touched_tests(INSIDE_FROM_HEADER) == {("tests.test_app", "test_long_one")}


def test_a_changed_decorator_counts_for_the_function_below_it():
    assert touched_tests(NEW_PARAM) == {("tests.test_parse", "test_parse")}


def test_async_tests_are_found():
    assert touched_tests(ASYNC_TEST) == {("tests.test_async", "test_waits")}


def test_fixture_changes_touch_no_test():
    assert touched_tests(FIXTURE_ONLY) == set()


def test_narrow_keeps_target_tests_and_moves_the_rest():
    flipped = ["tests.test_cli::test_new_option", "tests.test_cli::test_other", "tests.test_cli::test_old"]
    f2p, also, note = narrow(flipped, NEW_TEST)
    assert f2p == ["tests.test_cli::test_new_option"]
    assert also == ["tests.test_cli::test_old", "tests.test_cli::test_other"]
    assert note == ""


def test_narrow_matches_class_tests_and_all_parameters():
    flipped = ["testing.test_junitxml.TestPython::test_summing[a]",
               "testing.test_junitxml.TestPython::test_summing[b]",
               "testing.test_junitxml.TestPython::test_untouched"]
    f2p, also, _ = narrow(flipped, EDITED_METHOD)
    assert len(f2p) == 2 and also == ["testing.test_junitxml.TestPython::test_untouched"]


def test_a_same_named_test_in_another_file_is_not_a_target():
    flipped = ["tests.test_cli::test_new_option", "tests.test_other::test_new_option"]
    f2p, also, _ = narrow(flipped, NEW_TEST)
    assert f2p == ["tests.test_cli::test_new_option"] and also == ["tests.test_other::test_new_option"]


def test_falls_back_to_the_name_when_the_module_path_differs():
    # e.g. a project whose JUnit names don't start at the repository root
    flipped = ["cli::test_new_option", "cli::test_other"]
    f2p, also, _ = narrow(flipped, NEW_TEST)
    assert f2p == ["cli::test_new_option"] and also == ["cli::test_other"]


def test_keeps_everything_with_a_note_when_no_target_is_found():
    flipped = ["tests.test_a::test_x", "tests.test_a::test_y"]
    f2p, also, note = narrow(flipped, FIXTURE_ONLY)
    assert f2p == flipped and also == [] and "every flipped test" in note


def test_decide_narrows_and_still_verifies():
    result = {"status": "ran", "python_version": "3.12.1",
              "before": {"tests.test_cli::test_old": "error", "tests.test_cli::test_new_option": "error"},
              "after": {"tests.test_cli::test_old": "passed", "tests.test_cli::test_new_option": "passed"}}
    v = decide(result, NEW_TEST)
    assert v.status == "verified"
    assert v.fail_to_pass == ["tests.test_cli::test_new_option"]
    assert v.also_fixed == ["tests.test_cli::test_old"]


def make_record(f2p, also=(), status="verified", test_patch=NEW_TEST):
    return Record(id="o__r-1", repo="o/r", repo_url="https://github.com/o/r", license="MIT",
                  issue=Issue(1, "t", "b", "u"), pr=PullRequest(1, "t", "b", "u"),
                  base_commit="abc", fix_commit="def", code_patch="", test_patch=test_patch,
                  code_files=[], test_files=["tests/test_cli.py"],
                  verification=Verification(status=status, fail_to_pass=list(f2p), also_fixed=list(also)))


def test_refine_narrows_an_old_inflated_record():
    record = make_record(["tests.test_cli::test_new_option", "tests.test_cli::test_old"])
    assert refine(record) is True
    assert record.verification.fail_to_pass == ["tests.test_cli::test_new_option"]
    assert record.verification.also_fixed == ["tests.test_cli::test_old"]


def test_refine_twice_changes_nothing_the_second_time():
    record = make_record(["tests.test_cli::test_new_option", "tests.test_cli::test_old"])
    refine(record)
    assert refine(record) is False


def test_refine_skips_unverified_records():
    record = make_record(["tests.test_cli::test_old"], status="failed")
    assert refine(record) is False
    assert record.verification.fail_to_pass == ["tests.test_cli::test_old"]


def test_old_records_without_also_fixed_still_load():
    data = make_record(["x"]).to_dict()
    del data["verification"]["also_fixed"]
    assert Record.from_dict(data).verification.also_fixed == []
