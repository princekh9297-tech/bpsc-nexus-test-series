BPSC Nexus — Auto Question Layout Refinement

REPLACE ONLY THESE FILES in your existing repository:
1. question_bank.py
2. static/index.html

Do not replace miniapp.py for this refinement if you already deployed the previous Auto Question Layout package. Its API payloads already include question_type, question_structure and question_flags.

WHAT WAS FIXED
- The previous classifier often identified a stem as statement-based but could not split common labels such as (1), (2), 1., 2., (i), (ii), Statement I/II, and Hindi कथन 1/2. This caused the statement text to remain as one unstructured block.
- Broadened detection for phrasing such as “Which of the following statements…”, “Consider the following statements…”, and Hindi equivalents.
- Statement renderer now separates each recognized statement into its own card, in English and Hindi independently when bilingual text is available.
- The final instruction (e.g. “Which of the above statements is correct?”) is kept separate and is not swallowed into the final statement.
- Match-the-following supports List I/List II parsing with common (a)/(b) and (1)/(2) labels.
- Startup backfill now reclassifies existing records previously saved as 'standard', not only records marked 'unknown'. It syncs the new layout metadata to existing attempt snapshots.
- Existing question wording, options, correct answers, explanations, IDs, attempts and scores are not rewritten. No re-upload or database reset is required.
- If the structure is ambiguous, the question falls back to the normal MCQ view rather than displaying a misleading layout note.

DEPLOY
1. Back up the current two files locally.
2. Replace question_bank.py and static/index.html at the exact paths above.
3. Commit and push to GitHub; let the Mini App Render service redeploy.
4. Check Render logs for successful startup/schema verification.
5. Test at least one numbered statement question, one Roman numeral statement question, one bilingual statement question, one Match List I/List II question, and a standard MCQ.

VALIDATION IN THIS BUILD
- Python compilation passed for miniapp.py and question_bank.py.
- Full inline JavaScript syntax check passed.
- Regression smoke tests passed for English/Hindi statement cards, final instruction preservation, matching lists, Roman-numeral statements, and standard-MCQ fallback.
- No live Render/PostgreSQL integration test was performed in this workspace. The additive backfill executes on Mini App startup through question_bank.ensure_schema().
