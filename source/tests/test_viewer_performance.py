"""Static derived caches preserve filtering and validated media semantics."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


def function_source(name):
    source = (ROOT / "web/app.js").read_text()
    start = source.index("  function " + name + "(")
    end = source.find("\n  }", start)
    line_end = source.index("\n", start)
    if end == -1 or line_end < end and source[start:line_end].rstrip().endswith("}"):
        return source[start:line_end]
    return source[start:end + len("\n  }")]


@unittest.skipUnless(shutil.which("node"), "Node.js is required for viewer function tests")
class ViewerPerformanceTests(unittest.TestCase):
    def javascript(self, body, *, snapshot=True, source=None):
        functions = "\n".join(function_source(name) for name in (
            "safePath", "fileURL", "searchText", "localFilter", "evidencePaths",
            "staticAsset", "mediaURI", "resolveMediaURI", "includedPaths",
        ))
        script = "\n".join([
            "const assert = require('node:assert/strict');",
            "const snapshot = " + json.dumps(snapshot) + ";",
            "const source = " + json.dumps(source or {}, ensure_ascii=False) + ";",
            "const staticMedia = snapshot && ['static-media-v1','static-media-v2'].includes(source.format) && source.transport !== 'inline';",
            "const searchTexts = snapshot ? new WeakMap() : null;",
            "const evidenceLists = snapshot ? new WeakMap() : null;",
            "const includedLists = snapshot ? new WeakMap() : null;",
            "const mediaURIs = snapshot ? new WeakMap() : null;",
            "const state = {filters:{}};",
            "const dateOf = run => String(run.date || run.started_at || '').slice(0,10);",
            "const finite = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;",
            functions, body,
        ])
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_search_preserves_case_dates_and_excluded_reviews(self):
        self.javascript("""
const runs = [{id:'a',model:'Model-A',purpose:'benchmark',date:'2026-10-08',reviews:[{reviewer:'excluded-review-text'}]},
              {id:'b',model:'Model-B',purpose:'smoke',date:'2026-10-09'}];
state.filters = {q:'  MODEL-A  ',purpose:'benchmark',date_to:'2026-10-08'};
assert.deepEqual(localFilter(runs).map(r=>r.id), ['a']);
assert.equal(searchTexts.has(runs[0]), true);
assert.equal(searchTexts.has(runs[1]), false);
state.filters = {q:'excluded-review-text'};
assert.deepEqual(localFilter(runs), []);
state.filters = {date_from:'2026-10-09'};
assert.deepEqual(localFilter(runs).map(r=>r.id), ['b']);
""")

    def test_static_cached_paths_are_deduplicated_and_safe(self):
        self.javascript("""
const run={id:'a',evaluation:{evidence:['evidence/a.png',{path:'evidence/a.png'},'../b.png','/b.png','evidence/a.txt',null]}};
const before=JSON.stringify(run);
const paths=evidencePaths(run);
assert.deepEqual(paths,['evidence/a.png']);
assert.equal(evidencePaths(run),paths);
assert.equal(JSON.stringify(run),before);
""")

    def test_live_records_never_keep_stale_search_or_paths(self):
        self.javascript("""
const run={id:'a',model:'before',evaluation:{evidence:['evidence/a.png']}};
assert.equal(searchText(run).includes('before'),true);
assert.deepEqual(evidencePaths(run),['evidence/a.png']);
run.model='after';run.evaluation.evidence=['evidence/b.png'];
assert.equal(searchText(run).includes('after'),true);
assert.deepEqual(evidencePaths(run),['evidence/b.png']);
assert.equal(mediaURI(run,'evidence/a.png'),null);
assert.equal(mediaURI(run,'evidence/b.png'),'/api/runs/a/file?path=evidence%2Fb.png');
assert.deepEqual(includedPaths(run),[]);
""", snapshot=False)

    def test_cached_media_retains_mapping_and_thumbnail_validation(self):
        full = "media/" + "a" * 64 + ".jpg"
        thumb = "media/" + "b" * 64 + ".jpg"
        wrong = "media/" + "c" * 64 + ".jpg"
        data = {
            "format": "static-media-v2", "transport": "external",
            "evidence": {"a/evidence/a.png": full, "a/evidence/b.png": wrong},
            "thumbnails": {"a/evidence/a.png": thumb},
            "assets": {full: {"sha256": "a" * 64, "mime": "image/jpeg", "size": 9},
                       thumb: {"sha256": "b" * 64, "mime": "image/jpeg", "size": 8, "width": 481},
                       wrong: {"sha256": "d" * 64, "mime": "image/jpeg", "size": 9}},
        }
        self.javascript("""
const run={id:'a',evaluation:{evidence:['evidence/a.png','evidence/b.png']}};
assert.equal(mediaURI(run,'evidence/a.png'),source.evidence['a/evidence/a.png']);
assert.equal(mediaURI(run,'evidence/a.png',true),null);
assert.equal(mediaURI(run,'evidence/b.png'),null);
assert.equal(mediaURI(run,'../a.png'),null);
assert.equal(mediaURI(run,'evidence/unregistered.png'),null);
const paths=includedPaths(run);
assert.deepEqual(paths,['evidence/a.png']);
assert.equal(includedPaths(run),paths);
""", source=data)

    def test_static_search_text_created_only_once(self):
        self.javascript("""
let reads=0;const run={id:'a',get model(){reads++;return 'needle'}};
state.filters={q:''};localFilter([run]);assert.equal(reads,0);
state.filters={q:'needle'};assert.equal(localFilter([run]).length,1);
const first=reads;assert.ok(first>0);
assert.equal(localFilter([run]).length,1);assert.equal(reads,first);
""")


if __name__ == "__main__":
    unittest.main()
