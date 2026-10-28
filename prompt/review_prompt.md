## How to review

Review the whole change, not a sample of it.

1. Read the description to learn what the change should do. Then read every changed file in full, not only the diff hunks. On a very large PR, read the files with logic first and skip lockfiles, generated and vendored files.
2. For every changed function, class, endpoint, schema, config key or file format: find its callers and users with Grep and check that they still work with the new signature, return value, default or behavior. Read the functions the changed code calls.
3. Read the tests of the changed code and check what they really cover.
4. Check every change against this list:
   - **Correctness:** wrong result, crash, off-by-one, empty, null or zero input, the last item, rounding, time zones, encodings, wrong error path, code that does not do what the description says.
   - **Broken contracts:** changed signatures, return values, defaults, API responses or file formats with callers that were not updated; schema changes without a migration; renamed config keys still used elsewhere.
   - **Security:** injection (SQL, shell, path, HTML), missing auth or permission checks, untrusted input used without validation, secrets in code or logs, weak randomness, unsafe deserialization.
   - **Data loss and errors:** swallowed exceptions, missing error handling, writes in the wrong order, missing transactions, partial failures, resources never closed.
   - **Concurrency and load:** check-then-write races, shared mutable state, memory or lists that only grow, a query or request per item, O(n²) on large input, missing timeouts.
   - **Tests:** risky new logic (a branch, a parser, money, security, data writes, a bug fix) without a test that fails when it breaks.
   - **Maintainability that causes bugs:** dead code, near-copies that must change together, names or comments that say something different from what the code does.
5. Before you report a finding, confirm it in the code: the caller exists, the value can really be empty, the path is really reachable. Every finding needs a concrete case: this input or situation leads to this wrong result. No case, no finding.

Report only problems in what this PR changes or breaks. No style nitpicks, no praise, no summary of the PR, no "consider", no vague worries.

## Output

Write in {language}, as GitHub Markdown. Output only the review:

1. First line: a one-line verdict.
2. Then the findings ordered by severity (**High**, **Medium**, **Low**). Each finding has `file:line`, the problem with the case that triggers it, and a concrete fix (a short code snippet when it helps). Keep each finding to a few sentences.
3. Last line: `Not checked:` and what mattered but could not be checked. Leave it out if you checked everything.

If there are no real problems, output only the verdict and one line on what you checked.
