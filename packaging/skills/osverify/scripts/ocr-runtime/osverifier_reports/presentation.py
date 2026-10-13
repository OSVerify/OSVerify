"""Reversible, source-anchored typography edits for the reader-facing report."""

import re

# A token must not continue a word, number or numbered range: "line 12" is not
# inside "line 120", and "Lemma 2.1" does not verify "Lemma 2.17" or "Lemma 2.1.3".
_CONTINUES = r"(?:\w|[.\-–—]\w)"


def _token(value, flags=0):
    left = r"(?<!\w)" if re.match(r"\w", value) else ""
    right = r"(?!" + _CONTINUES + ")" if re.search(r"\w$", value) else ""
    return re.compile(left + re.escape(value) + right, flags)


def presented_feedback(source, issue_id, placement):
    text = source["feedback"][placement.ref]["text"]
    patches = []
    for edit in placement.presentation:
        # Occurrences count whole-token matches, never a prefix/suffix of a longer token.
        matches = list(_token(edit.original).finditer(text)) if edit.original else []
        if not edit.original or edit.occurrence >= len(matches):
            raise ValueError("Presentation edit is not anchored in the released feedback")
        match = matches[edit.occurrence]
        if edit.kind == "math":
            if not (edit.replacement.startswith("$") and edit.replacement.endswith("$")):
                raise ValueError("Math presentation needs dollar delimiters")
            inner = edit.replacement[1:-1]
            # Delimiters and subscript grouping only: no mathematical rewrite.
            def canonical(value):
                value = re.sub(r"([_^])\{([^{}])\}", r"\1\2", value)
                return re.sub(r"\s", "", value)

            if "$" in inner or canonical(inner) != canonical(edit.original):
                raise ValueError("Math presentation must retain the original expression")
        else:
            loc = re.fullmatch(r"lines? (\d+)(?:[–—-](\d+))?", edit.original, re.I)
            if not loc:
                raise ValueError("Location edit must replace an internal line reference")
            a, b = int(loc[1]), int(loc[2] or loc[1])
            candidates = source.get("paper_locations", {}).get("issues", {}).get(issue_id, {}).get("passages", [])
            refs = []
            for candidate in candidates:
                span = candidate["source_span"]
                if span["document"] == "candidate_solution" and span["start_line"] <= a <= b <= span["end_line"]:
                    # Require a printed locator covering the entire selected span.
                    per_line = [candidate["line_references"].get(str(n), []) for n in range(a, b + 1)]
                    refs = [ref for ref in per_line[0] if all(ref in row for row in per_line)]
                    if refs:
                        break
            if not refs or not any(_token(ref, re.I).search(edit.replacement) for ref in refs):
                raise ValueError("Location presentation needs a verified printed reference")
            if re.search(r"\blines?\s+\d", edit.replacement, re.I):
                raise ValueError("Reader locator still contains an internal line number")
        patches.append((match.start(), match.end(), edit.replacement))
    patches.sort()
    if any(a[1] > b[0] for a, b in zip(patches, patches[1:])):
        raise ValueError("Presentation edits overlap")
    for a, b, replacement in reversed(patches):
        text = text[:a] + replacement + text[b:]
    return text
