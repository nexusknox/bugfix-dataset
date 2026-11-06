"""Browse the dataset: streamlit run viewer/app.py

Everything shown comes from people on GitHub, so it's displayed as plain
text or code, never rendered as HTML or Markdown.
"""

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bugfix import store  # noqa: E402
from bugfix.browse import filter_records, row, summary  # noqa: E402

DATA = Path("data/candidates.jsonl")

st.set_page_config(page_title="Bug-fix dataset", layout="wide")
st.title("Verified bug-fix dataset")
st.caption("Real bugs, their fixes, and proof: the new tests fail before the fix and pass after it. "
           "Records support human review; check anything you rely on.")


@st.cache_data
def load(path: str, modified: float):
    return store.load(Path(path))


if not DATA.exists():
    st.info("No records yet. Run: python -m bugfix.cli mine owner/repo")
    st.stop()

records = load(str(DATA), DATA.stat().st_mtime)

with st.sidebar:
    st.header("Filter")
    repos = st.multiselect("Repository", sorted({r.repo for r in records}))
    statuses = st.multiselect("Verification", ["verified", "failed", "error", "not_run", "timeout"],
                              default=["verified"])
    min_score = st.slider("Minimum quality", 0, 100, 0, step=5)
    search = st.text_input("Search issue / PR text")

shown = filter_records(records, repos, statuses, min_score, search)
numbers = summary(records)
c1, c2, c3, c4 = st.columns(4)
c1.metric("Records", numbers["records"])
c2.metric("Verified", numbers["verified"], help="Tests fail before the fix and pass after it")
c3.metric("Verified rate", f"{numbers['verified_rate']}%", help="Of the records that have been tried")
c4.metric("Average quality", f"{numbers['average_quality']}/100")

st.subheader(f"{len(shown)} matching record(s)")
if not shown:
    st.stop()
st.dataframe([row(r) for r in shown], width="stretch", hide_index=True)

chosen_id = st.selectbox("Open a record", [r.id for r in shown])
record = next(r for r in shown if r.id == chosen_id)
v = record.verification

st.markdown(f"### {record.id}")
st.write(f"**{record.repo}** · licence {record.license} · quality {record.quality.score}/100 · "
         f"verification: {v.status}" + (f" ({v.note})" if v.note else ""))
st.write(f"Issue: {record.issue.url}  ·  Fix: {record.pr.url}")

left, right = st.columns(2)
with left:
    st.markdown("**Issue (the defect ticket)**")
    st.text(record.issue.title)
    st.code(record.issue.body or "(no description)", language=None, wrap_lines=True)
with right:
    st.markdown("**Quality score**")
    st.code("\n".join(record.quality.reasons), language=None)
    st.markdown(f"**Fail → pass ({len(v.fail_to_pass)})**")
    st.code("\n".join(v.fail_to_pass) or "-", language=None)
    if v.also_fixed:
        with st.expander(f"Also fixed ({len(v.also_fixed)}): flipped, but not tests the PR added or edited"):
            st.code("\n".join(v.also_fixed), language=None)
    if v.note:
        st.caption(v.note)
    st.caption(f"Python {v.python_version or '?'} · base commit {record.base_commit[:10]} · "
               f"{len(v.pass_to_pass)} other test(s) still pass")

st.markdown("**The fix**")
st.code(record.code_patch, language="diff")
st.markdown("**The new tests**")
st.code(record.test_patch, language="diff")
with st.expander("Test log: before the fix"):
    st.code(v.log_before or "-", language=None)
with st.expander("Test log: after the fix"):
    st.code(v.log_after or "-", language=None)
