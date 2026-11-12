BPSC Nexus Mini App JavaScript error fix

Replace only:
  static/index.html

Changes:
1. Added the missing ensureDemoGate() function used by existing quiz/question-bank actions.
2. Made ensureDemoGate compatible with this served frontend: it uses existing hasAdminAccess(), api(), loadAccess(), and showError() helpers, without calling absent demo-modal/banner functions.
3. Moved the profile-registration script after the profile drawer markup so its inputs exist before initialization.
4. Added null checks around optional profile input assignments.

Validation:
- All inline JavaScript parsed successfully with Node.js.
- Confirmed ensureDemoGate has exactly one definition.
- Confirmed the profile drawer markup appears before the profile script.

No backend, database, or other project files are changed.
