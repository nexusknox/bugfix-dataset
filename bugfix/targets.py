"""Which tests is a fix actually about?

A test "flips" when it doesn't pass without the fix and passes with it. Most
flips are the tests the pull request added or changed. But if the new tests
import something that only exists after the fix, the whole test file can't
load before it, so every test in that file flips, including dozens that have
nothing to do with the bug.

So we read the test patch, find the test functions it adds or edits, and
keep only those as fail_to_pass. Other flipped tests are kept separately as
also_fixed, so nothing is thrown away.
"""

import re

from bugfix.rules import split_diff

DEF = re.compile(r"^(?P<indent>\s*)(?:async\s+)?def\s+(?P<name>\w+)\s*\(")
HUNK = re.compile(r"^@@ [^@]* @@ ?(?P<context>.*)$")


def _module(path: str) -> str:
    """'testing/test_junitxml.py' -> 'testing.test_junitxml'."""
    return path[:-3].replace("/", ".") if path.endswith(".py") else ""


def _touched_in_file(lines: list[str]) -> set[str]:
    """Names of test functions with an added, removed or edited line."""
    touched: set[str] = set()
    current, current_indent = None, -1   # the test function we're inside
    in_decorator = False                  # between "@..." and the def it decorates
    decorator_changed = False             # a changed decorator belongs to the next def

    def enter(line: str) -> bool:
        nonlocal current, current_indent
        match = DEF.match(line)
        if not match:
            return False
        name = match["name"]
        current = name if name.startswith("test") else None
        current_indent = len(match["indent"])
        return True

    for line in lines:
        hunk = HUNK.match(line)
        if hunk:
            # git names the function the hunk starts in, e.g. "@@ -10,6 +10,9 @@ def test_x():"
            current, current_indent, decorator_changed, in_decorator = None, -1, False, False
            enter(hunk["context"])
            continue
        if line.startswith(("+++", "---")) or line[:1] not in (" ", "+", "-"):
            continue
        changed, text = line[0] in "+-", line[1:]
        if not text.strip():
            continue
        if enter(text):
            if current and (changed or decorator_changed):
                touched.add(current)
            decorator_changed = in_decorator = False
            continue
        if in_decorator and text.lstrip().startswith("class "):
            in_decorator = decorator_changed = False  # a decorated class, not a test function
        if in_decorator:                          # e.g. a parametrize list over several lines
            decorator_changed = decorator_changed or changed
            continue
        indent = len(text) - len(text.lstrip())
        if text.lstrip().startswith("@"):
            if indent <= current_indent or current_indent < 0:
                current, current_indent = None, -1   # a decorator starts the next function
            decorator_changed = decorator_changed or changed
            in_decorator = True
            continue
        if current and indent <= current_indent:
            current, current_indent = None, -1        # back out of the function
        if changed and current:
            touched.add(current)
    return touched


def touched_tests(test_patch: str) -> set[tuple[str, str]]:
    """(module, test name) for every test the patch adds or edits."""
    found = set()
    for file in split_diff(test_patch):
        if not file.path.endswith(".py"):
            continue
        for name in _touched_in_file(file.text.splitlines()):
            found.add((_module(file.path), name))
    return found


def _split_id(test_id: str) -> tuple[str, str]:
    """'testing.test_x.TestA::test_b[1-2]' -> ('testing.test_x.TestA', 'test_b')."""
    classname, _, name = test_id.rpartition("::")
    return classname, name.split("[", 1)[0]


def _matches(test_id: str, targets: set[tuple[str, str]], by_module: bool) -> bool:
    classname, name = _split_id(test_id)
    for module, target in targets:
        if name != target:
            continue
        if not by_module or classname == module or classname.startswith(module + ".") \
                or classname.endswith("." + module) or ("." + module + ".") in classname:
            return True
    return False


def narrow(flipped: list[str], test_patch: str) -> tuple[list[str], list[str], str]:
    """(fail_to_pass, also_fixed, note) from all the tests that flipped.

    Tries the test's file and name first, then the name alone. If neither
    matches anything, every flipped test is kept, with a note saying so.
    """
    targets = touched_tests(test_patch)
    for by_module in (True, False):
        chosen = [t for t in flipped if _matches(t, targets, by_module)]
        if chosen:
            others = [t for t in flipped if t not in set(chosen)]
            return sorted(chosen), sorted(others), ""
    if not flipped:
        return [], [], ""
    return sorted(flipped), [], "couldn't tell which tests the patch targets, so every flipped test is kept"
