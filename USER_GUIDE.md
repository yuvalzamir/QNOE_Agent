# QNOE Lab Agent — User Guide

*A short, practical guide for lab members. You talk to the agent in Microsoft
Teams, in plain language — no commands to memorise. This guide is a work in
progress; more sections will be added.*

---

## Finding a file or document — just say "find"

When you want to **locate a file** — a notebook, script, paper, manual, dataset,
presentation, or any document, **including ones stored on SharePoint** — ask for
it in ordinary words. Useful trigger words: *find, locate, where is, give me,
show me, do we have*.

Examples:

- "**find** the SpectroMag setup document"
- "**where is** the SLG07 analysis notebook?"
- "**give me** the manual for the ANC350"
- "**do we have** a document about the needle-valve calibration?"

The agent searches **both** the lab data server *and* SharePoint, and replies with
each match's location:

- a **file path** for files on the lab server / in the code repositories, e.g.
  `/ICFO/groups/NOE/Setups/…/notebook.ipynb`
- a **clickable web link** for SharePoint files.

### Tips for good results

- **Use a distinctive keyword** — a device or sample code (`SLG07`, `BSCCO`,
  `SpectroMag`), a project name, or part of the filename. Vague requests
  ("find a document about physics") won't narrow down well.
- It matches on the **file name and folder path**, not the text inside files. If
  you want "*which files mention X inside them*", just ask the question normally
  and the agent will answer from its knowledge base.
- It finds **indexed** files only (supported document/code types). Something that
  was never ingested — some spreadsheets, images, raw binaries — may not appear.
- SharePoint files are **not** on a mounted drive, but they **are** searchable
  this way. If the agent ever says a SharePoint document is "out of reach," that's
  a bug — let the maintainer know.

---

## Looking up a measurement run (QCoDeS)

Every QCoDeS measurement has a **run id**. Ask about one directly:

- "what parameters were recorded in **run 848**?"
- "**how many databases** contain a run with ID 159?"
- "what was the **most recent gate sweep** in the L110 QTM setup?"

### Important: run ids are *per-database*

The same run number (say **159**) exists in **many different databases** as
completely unrelated measurements — each `.db` file numbers its runs from 1. So
when you ask about a bare run id, the agent will tell you **how many databases**
contain it and show a sample, then ask you to narrow down. To pin down the one
you mean, add context:

- **a sample:** "runs on sample `BFNB4`"
- **a setup or database:** "gate sweeps in the **L110 QTM room-T** setup"
- **a swept parameter:** "the most recent run that **swept the gate voltage**"
- **a date:** "runs from June 2025"

### What you get back

For a run, the agent reports its **run name**, the **swept** and **measured**
parameters, the sample, the database, and the timestamp — read straight from the
measurement registry (not guessed).

If a run id **doesn't exist**, the agent will say so plainly. It will **not**
invent parameters or results — if you see specific numbers for a run that
shouldn't exist, treat it with suspicion and tell the maintainer.

---

*Questions or something behaving oddly? Note the exact message you sent and the
reply, and pass it to the maintainer — that's exactly how issues get found and
fixed.*
