"""Pure timeline layout rules: real run dates only, missing scores stay missing."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def function_source(name):
    source = (ROOT / "web/app.js").read_text()
    start = source.index("  function " + name + "(")
    end = source.find("\n  }", start)
    line_end = source.index("\n", start)
    if end == -1 or line_end < end and source[start:line_end].rstrip().endswith("}"):
        return source[start:line_end]
    return source[start:end + len("\n  }")]


@unittest.skipUnless(shutil.which("node"), "Node.js is required for timeline function tests")
class TimelineTests(unittest.TestCase):
    def run_node(self, body):
        functions = "\n".join(function_source(name) for name in (
            "timelineDate", "timelineOffsets", "timelineColumns", "timelineLensValue",
        ))
        script = "\n".join([
            "const assert = require('node:assert/strict');",
            "const scoreKeys = ['compliance','recognizability','motion','visual_detail','interaction','completeness'];",
            "const dateOf = run => String(run.date || run.started_at || '').slice(0, 10);",
            "const timelineBeadSize = 42;",
            functions,
            body,
        ])
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_dates_with_runs_take_width(self):
        self.run_node("""
          const runs = [{date:'2026-09-30'},{date:'2026-10-08'},{date:'2026-10-17'}];
          const layout = timelineColumns(runs);
          assert.deepEqual(layout.dates, ['2026-09-30','2026-10-08','2026-10-17']);
          assert.equal(layout.columns.length, 3);
          assert.ok(layout.columns[1].start >= layout.columns[0].start + layout.columns[0].width - 0.01);
          assert.ok(layout.columns[1].center > layout.columns[0].center);
          assert.ok(layout.columns[2].center > layout.columns[1].center);
          assert.ok(layout.width >= 920);
          assert.equal(timelineColumns([]), null);
          assert.equal(timelineColumns([{date:'not-a-date'}]), null);
        """)

    def test_same_day_cluster_widens_its_column(self):
        self.run_node("""
          const one = timelineColumns([{date:'2026-10-08',tool:'t',model:'m'}]);
          const three = timelineColumns([
            {date:'2026-10-08',tool:'t',model:'m'},
            {date:'2026-10-08',tool:'t',model:'m'},
            {date:'2026-10-08',tool:'t',model:'m'},
          ]);
          assert.ok(three.columns[0].width > one.columns[0].width);
          assert.equal(three.columns[0].count, 3);
          assert.ok(three.columns[0].width >= 3 * 42 + 2 * 8);
        """)

    def test_offsets_are_horizontal_and_unique(self):
        self.run_node("""
          assert.deepEqual(timelineOffsets(1), [0]);
          assert.deepEqual(timelineOffsets(2), [-25, 25]);
          assert.deepEqual(timelineOffsets(3), [-50, 0, 50]);
          for (const count of [1, 2, 3, 4]) {
            const offsets = timelineOffsets(count);
            assert.equal(offsets.length, count);
            assert.equal(new Set(offsets).size, count);
          }
        """)

    def test_missing_and_multiple_scopes_are_not_merged(self):
        self.run_node("""
          const sheet = values => ({values});
          const full = sheet([4, 4, 3, 4, 4, 4]);
          assert.equal(timelineLensValue([full], 'motion'), 3);
          assert.equal(timelineLensValue([sheet([4, 4, null, 4, 4, 4])], 'motion'), null);
          assert.equal(timelineLensValue([], 'motion'), null);
          assert.equal(timelineLensValue([full, sheet([4, 4, 2, 4, 3, 4])], 'motion'), null);
          assert.equal(timelineLensValue([full, full], 'motion'), null);
          assert.equal(timelineLensValue([full], 'unknown'), null);
        """)


if __name__ == "__main__":
    unittest.main()
