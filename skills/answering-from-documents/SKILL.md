---
name: answering-from-documents
description: How to answer from the user's own files (the library) with checked citations, adding the web only for what they don't have. Use for any question their files may answer, named or not, and any question about their documents, contracts, reports, spreadsheets or slides.
---

# Answering from documents

The user's files are in the library (list_documents). Answer only from what they say, and
prove every claim with a quote that cite_answer accepts.

## Where to look

Split the question into the facts it needs ("is our late fee above the legal cap?": our fee
from their files, the cap maybe from the web). Look for every fact in the files first; use
the web only for what they don't have or what must be current, and cite the page's url and
an exact quote. On the user's own situation the files win over the web; say what both say.
When you speak, keep the sources apart: "your policy says 5%; the regulator's site says 3%".

## Find it

1. If you don't know which file, list_documents.
2. search_documents with the user's key words. When results are thin or off, search again:
   synonyms ("cancel": terminate, termination, end), the document's likely wording, a
   shorter query, one document at a time. Two or three searches is normal.
3. read_document at each promising location, the whole page or section, not just the
   passage: definitions, exceptions and conditions are often a few lines away. Read the
   next page when a clause continues.

## Get it right

- Answer from the text you read, never from memory or general knowledge. If you add
  general knowledge, say plainly that it isn't from their documents.
- Numbers: copy them exactly with their units and dates. For anything to add up, compare
  or compute in a spreadsheet or CSV, calculate it in code from the original file (its
  path is the `file` in list_documents), e.g. `uv run --with pandas,openpyxl python -c "..."`,
  and cite the sheet and the cells or rows you used.
- Figures, charts or a page that reads badly: look at it yourself with the Read tool, on
  the `file` (a PDF page) or the `pictures` read_document gives, and say what you saw. A
  spreadsheet cell showing a formula ("=SUM(B2:B9)") had no saved result: work it out.
- Scanned pages (marked scanned) can hold transcription mistakes: prefer a text page when
  both say the same, and say "from the scanned copy" when you rely on one.
- Several documents disagree, or a document contradicts itself: say so and cite both.
- Files still being read (list_documents says so): if you haven't found it, say you're
  still reading them, not that it isn't there.
- Not there: cite_answer with found: false, then say "That isn't in your documents" and
  what you searched. Never guess.

## Whole-document questions

For a summary or "all the risks", read section by section (read_document by page ranges
or headings), keep short notes with locations in a file in your project folder as you go,
then write the answer from the notes, each point with its citation.

## Cite and check

1. Call cite_answer with the answer and, for every claim, a citation: the document, the
   location as read_document gave it ("page 12", "§ 14. Termination", "sheet Sales"), and
   a quote copied exactly from that location (a full phrase or sentence, not a paraphrase).
   A fact from the web: its url and an exact quote from that page, read with
   read_web_page (WebFetch summarizes, so its words won't match the page).
2. If it rejects a citation, read that location again, copy the quote exactly or find the
   right location, or drop the claim; then call it again. You can't finish on an answer
   it hasn't accepted.
3. Your spoken answer: the finding in a sentence or two and where it is ("page 12 of the
   lease"), "from the scanned copy" when it told you so, and that the full answer with
   quotes is on the apps page (the page it returned).
