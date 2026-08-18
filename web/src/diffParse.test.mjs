import test from 'node:test';
import assert from 'node:assert/strict';
import { deriveSplitRows, parseUnifiedDiff } from './diffParse.ts';

const PATCH = `diff --git a/src/example.ts b/src/example.ts
index 1111111..2222222 100644
--- a/src/example.ts
+++ b/src/example.ts
@@ -10,5 +10,6 @@ function example() {
 context
-old one
-old two
+new one
+new two
+new three
 tail
\\ No newline at end of file
`;

test('parseUnifiedDiff preserves paths, counters, and old/new line numbers', () => {
  const parsed = parseUnifiedDiff(PATCH);
  assert.equal(parsed.parseError, null);
  assert.equal(parsed.files.length, 1);
  const file = parsed.files[0];
  assert.equal(file.oldPath, 'src/example.ts');
  assert.equal(file.newPath, 'src/example.ts');
  assert.equal(file.deletions, 2);
  assert.equal(file.additions, 3);
  assert.deepEqual(
    file.hunks[0].lines.map(({ type, oldNumber, newNumber }) => ({ type, oldNumber, newNumber })),
    [
      { type: 'context', oldNumber: 10, newNumber: 10 },
      { type: 'del', oldNumber: 11, newNumber: null },
      { type: 'del', oldNumber: 12, newNumber: null },
      { type: 'add', oldNumber: null, newNumber: 11 },
      { type: 'add', oldNumber: null, newNumber: 12 },
      { type: 'add', oldNumber: null, newNumber: 13 },
      { type: 'context', oldNumber: 13, newNumber: 14 },
      { type: 'meta', oldNumber: null, newNumber: null },
    ],
  );
});

test('deriveSplitRows mirrors context and zips adjacent delete/add runs', () => {
  const lines = parseUnifiedDiff(PATCH).files[0].hunks[0].lines;
  const rows = deriveSplitRows(lines);
  assert.equal(rows.length, 6);
  assert.equal(rows[0].type, 'pair');
  assert.equal(rows[0].oldLine, rows[0].newLine);
  assert.deepEqual(
    rows.slice(1, 4).map((row) => row.type === 'pair'
      ? [row.oldLine?.text ?? null, row.newLine?.text ?? null]
      : null),
    [
      ['old one', 'new one'],
      ['old two', 'new two'],
      [null, 'new three'],
    ],
  );
  assert.equal(rows[4].type, 'pair');
  assert.equal(rows[4].oldLine, rows[4].newLine);
  assert.equal(rows[5].type, 'meta');
});

test('deriveSplitRows leaves standalone additions on the new side and metadata spanning', () => {
  const lines = [
    { type: 'add', oldNumber: null, newNumber: 4, text: 'added' },
    { type: 'meta', oldNumber: null, newNumber: null, text: '\\ No newline at end of file' },
  ];
  const rows = deriveSplitRows(lines);
  assert.deepEqual(rows[0], { type: 'pair', oldLine: null, newLine: lines[0] });
  assert.deepEqual(rows[1], { type: 'meta', line: lines[1] });
});

test('deriveSplitRows keeps replacement paired across no-newline metadata', () => {
  const lines = [
    { type: 'del', oldNumber: 7, newNumber: null, text: 'before' },
    { type: 'meta', oldNumber: null, newNumber: null, text: '\\ No newline at end of file' },
    { type: 'add', oldNumber: null, newNumber: 7, text: 'after' },
  ];
  const rows = deriveSplitRows(lines);
  assert.deepEqual(rows[0], { type: 'pair', oldLine: lines[0], newLine: lines[2] });
  assert.deepEqual(rows[1], { type: 'meta', line: lines[1] });
});

test('parseUnifiedDiff covers added, deleted, renamed, binary, and quoted UTF-8 paths', () => {
  const patch = `diff --git a/new.txt b/new.txt
new file mode 100644
--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+new
diff --git a/old.txt b/old.txt
deleted file mode 100644
--- a/old.txt
+++ /dev/null
@@ -1 +0,0 @@
-old
diff --git a/before.txt b/after.txt
similarity index 100%
rename from before.txt
rename to after.txt
diff --git a/image.png b/image.png
Binary files a/image.png and b/image.png differ
diff --git "a/\\344\\270\\255\\346\\226\\207.py" "b/\\344\\270\\255\\346\\226\\207.py"
--- "a/\\344\\270\\255\\346\\226\\207.py"
+++ "b/\\344\\270\\255\\346\\226\\207.py"
@@ -1 +1 @@
-old
+new
`;
  const parsed = parseUnifiedDiff(patch);
  assert.equal(parsed.parseError, null);
  assert.deepEqual(parsed.files.map((file) => file.status), [
    'added', 'deleted', 'renamed', 'binary', 'modified',
  ]);
  assert.equal(parsed.files[4].oldPath, '中文.py');
  assert.equal(parsed.files[4].newPath, '中文.py');
});

test('parseUnifiedDiff identifies numstat summaries without inventing hunks', () => {
  const parsed = parseUnifiedDiff('2\t1\tsrc/example.ts\n');
  assert.equal(parsed.truncated, true);
  assert.match(parsed.parseError, /numstat/);
  assert.deepEqual(parsed.files, []);
});
