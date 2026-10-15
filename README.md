<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/osverify-dark.png">
    <img alt="OSVerify" src="docs/assets/osverify-light.png" width="420">
  </picture>
</p>

<p align="center"><b>One-sided verification under limited expert review</b></p>

<p align="center">
  <a href="https://arxiv.org/abs/2610.18089">Paper</a> ·
  <a href="https://osverify.github.io">Website</a> ·
  <a href="https://github.com/OSVerify/Rethlas-OSV">Rethlas-OSV desktop app (beta)</a> ·
  <a href="docs/guide.md">Guide</a>
</p>

OSVerify reviews mathematical solutions and research papers and produces audited
feedback for human review. It is a one-sided verifier: its goal is to reject as many
invalid candidates as possible while keeping false rejections of valid ones low,
so that expert time goes to the candidates most likely to be correct.

Verifying a specific error is often easier than discovering one, so OSVerify decouples
**error proposal**, which searches the whole text and its segments, from
**error validation**, which separates substantial errors
from minor or hallucinated ones. Only feedback that an independent auditor approves word
for word is released, and the work under review is never rewritten. Passing review means
*ready for expert confirmation*, not *certified correct*. OSVerify has been used to find
errors in published research papers that were then confirmed by their authors, and to
help resolve open conjectures.

**Scope.** OSVerify currently supports mathematics: complete or partial solutions to a
stated problem, and research papers in LaTeX, Markdown or PDF. The method is not specific
to mathematics; extending it to other scientific fields is future work, and contributions
are welcome.

## Install

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and the
[Codex CLI](https://github.com/openai/codex) 0.162 or newer, signed in with your own account
(older CLIs cannot use current models such as gpt-6-astra).
PDF inputs also require Poppler; generating referee-report PDFs requires XeLaTeX or LuaLaTeX.

```bash
git clone https://github.com/OSVerify/OSVerify && cd OSVerify
uv sync
uv run osverify demo --run-dir runs/demo   # offline walkthrough, no model calls
```

Open `runs/demo/report.md` to see the output of a scripted review.

## Install the agent skill

The same reviewer is packaged as an agent skill with the same commands. Download it from
the latest release and unzip it into your agent's skills folder:

```bash
curl -LO https://github.com/OSVerify/OSVerify/releases/latest/download/osverify.zip
unzip osverify.zip -d ~/.codex/skills       # Codex; for Claude Code use ~/.claude/skills
```

To build it from a checkout instead, run `uv run python packaging/build_skill.py`
(it writes `dist/osverify.zip`).

Then ask the agent, for example: *"Use $osverify to review paper.pdf."* The skill runs the
bundled engine with `uv` and calls your signed-in Codex CLI, so the requirements above
still apply.

## Review a solution

```bash
uv run osverify verify --question question.md --solution solution.md \
  --decompose --run-dir runs/my-solution
```

A completed full-solution review returns one decision. Read the status and audited
feedback in `runs/my-solution/report.md`; an interrupted review may have no decision yet.

| Decision | Meaning |
| --- | --- |
| `READY_FOR_HUMAN` | Finished with nothing outstanding; hand off for final human confirmation. |
| `REVISE` | Apply the released feedback: fixable errors, omissions or needed clarifications. |
| `REJECT` | An audited blocking error or demonstrated missing core obligation warrants rejection. |

Add `--mode partial` to check unfinished progress against the original question.
A clean partial review returns `PASS` instead of `READY_FOR_HUMAN`; see the
[guide](docs/guide.md#solution-decisions) for the meaning of each result.

## Review a paper

```bash
uv run osverify verify --review-type paper --paper paper.tex \
  --decompose --run-dir runs/my-paper
```

- The paper can be LaTeX, Markdown or PDF. A PDF is first transcribed page by page with
  independent checks, and every reported issue is re-checked against the original page.
- Without `--question`, the results to review are chosen automatically:
  `--targets smart` (default: the main theorems and the key steps they rely on),
  `lazy` (results stated in the introduction), `manual --labels thm:main ...`, or `full`.
- Paper reports show findings and coverage, without an overall solution verdict.
  Issues are grouped as Major, Minor or Unclassified using a
  [repair-based classification](docs/guide.md#paper-findings).
- A referee report PDF is generated when reporting and compilation succeed.
  Add `--no-report` to skip it; `report.md` remains available in the run directory.

## Models

The default model is `gpt-6-astra`. `uv run osverify models` lists the models your Codex
account offers; choose one with `--model`, for example `--model gpt-6.1-sol` or
`--model gpt-5.6-sol`.

## Commands

| Command | What it does |
| --- | --- |
| `osverify verify` | Review a solution, or a paper with `--review-type paper` |
| `osverify ocr` | Transcribe a PDF on its own; reuse it with `verify --ocr-dir` |
| `osverify report` | Draft a referee report from a saved, stopped review |
| `osverify export` | Export a review as LaTeX or Markdown, without model calls |
| `osverify models` | List the models you can choose with `--model` |
| `osverify demo` | Offline scripted demonstration |

Rerun the same command to resume an interrupted review. Every input, prompt and model
call is kept in the run directory. See `uv run osverify <command> --help` for all options.

## Documentation

- [Guide](docs/guide.md): run a review, read results, choose options and resume
- [Runtime reference](docs/runtime-reference.md): internals, configuration and Python integration
- [Output contract](docs/output-contract.md): what each report field means
- [Literature tools](docs/literature-tools.md): how cited references are retrieved and checked
- [Development notes](AGENTS.md)

## Citation

If you use OSVerify in your research, please cite our [paper](https://arxiv.org/abs/2610.18089):

```bibtex
@misc{nie2026osverify,
  title         = {One-Sided Verification Under Limited Expert Review},
  author        = {Nie, Fan and Ye, Haotian and Liu, Shurui and Wang, Zihao and Ermon, Stefano and Zou, James},
  year          = {2026},
  eprint        = {2610.18089},
  archivePrefix = {arXiv},
  primaryClass  = {cs.AI},
  url           = {https://arxiv.org/abs/2610.18089}
}
```

## License

Apache License 2.0; see [LICENSE](LICENSE). Third-party attribution is in
[NOTICE.md](NOTICE.md).
