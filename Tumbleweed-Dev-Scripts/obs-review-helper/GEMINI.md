# openSUSE OBS Submit Request Review Workspace

Welcome! This workspace is set up to assist in reviewing submit requests targeting openSUSE:Factory against the official openSUSE packaging guidelines.

## 🛠️ Core Tooling & Verification Engine

We have developed a custom review helper utility in Python: `osc_review_helper.py`.

### Usage
- **List pending reviews** for the group (defaults to `opensuse-review-team` and `openSUSE:Factory`):
  ```bash
  python3 osc_review_helper.py list
  ```
- **Show request details and full unified diff**:
  ```bash
  python3 osc_review_helper.py show <request_id>
  ```
- **Run automated compliance checks** (the script scans the diff for guideline issues):
  ```bash
  python3 osc_review_helper.py verify <request_id>
  ```
- **Post a review comment to OBS**:
  ```bash
  python3 osc_review_helper.py comment <request_id> "your comment"
  ```
- **Accept a pending review on OBS**:
  ```bash
  python3 osc_review_helper.py accept <request_id> ["optional comment message"]
  ```

---

## 📋 The openSUSE Review Checklist (Guidelines)

During review, the helper script automatically flags low-level discrepancies, but you must combine it with a cognitive review of the diff using this checklist:

### 1. Spec File Header & Structure
- Must have the standard header pointing to `https://bugs.opensuse.org/` and the copyright boilerplate.
- Obsolete declarations must **NOT** be present. If found, flag them:
  - `BuildRoot: %{_tmppath}/...` is obsolete.
  - `%clean` section (containing `rm -rf %{buildroot}`) is obsolete.
  - `%defattr(-,root,root,-)` is obsolete in `%files` sections.

### 2. Path Macroization
- Ensure hardcoded system paths are replaced with standard RPM macros:
  - `/usr/bin/` $\rightarrow$ `%{_bindir}`
  - `/usr/sbin/` $\rightarrow$ `%{_sbindir}`
  - `/etc/` $\rightarrow$ `%{_sysconfdir}`
  - `/usr/share/` $\rightarrow$ `%{_datadir}`
  - `/usr/include/` $\rightarrow$ `%{_includedir}`

### 3. Changes Attribution (`.changes` file)
- Every request must include or modify a `.changes` file with a properly formatted header:
  `Day Month Date Hour:Min:Sec UTC Year - Name <email>`
- The entry must clearly document the version updates, security CVEs (using `[bsc#...]` and `CVE-XXXX-XXXX`), and packaging changes.

### 4. Patch Life Cycle Documentation (Strict Guideline)
- According to the [openSUSE Patch Life Cycle Guidelines](https://en.opensuse.org/openSUSE:Packaging_Patches_guidelines#Patch_life_cycle), **every patch addition, modification, or removal must be explicitly documented in the `.changes` file, including the exact patch filename**.
- The script automatically verifies that any patch added or dropped in the `.spec` diff has its filename mentioned inside the `.changes` diff. If missing, flag this discrepancy in your comment!

### 5. Shared Library Policy
- If a shared library (`libre`) gets an SOVERSION bump (e.g. `sover 43` $\rightarrow$ `sover 44`), ensure the subpackage is renamed accordingly (e.g. `libname` becomes `libre44`) and properly handled using `%ldconfig_scriptlets`.

---

## 🤖 Hybrid Review Workflow (How to Review)

When you are asked to review requests, follow this step-by-step workflow:

1. **List requests**: Run `python3 osc_review_helper.py list` to see what's pending.
2. **Verify request**: Run `python3 osc_review_helper.py verify <id>` to check for low-level static violations.
3. **Show details**: Run `python3 osc_review_helper.py show <id>` to inspect the description, active reviews, and full diff.
4. **Cognitive Synthesis & Collaborative Verification**:
   - Check if there are any CVE/security vulnerabilities solved.
   - Run the checks from the openSUSE checklist.
   - **STRICT PEER-REVIEW MANDATE:** You are operating as a collaborative peer programmer. You **must** present your findings, detailed diff analysis, and proposed review comment text to the user first. **Do not post any comments on OBS until the user has reviewed and explicitly agreed to your assessment.**
   - Once agreed:
     - If the package is clean, directly accept the review using:
       ```bash
       python3 osc_review_helper.py accept <request_id> "accepted by gemini"
       ```
     - If there are discrepancies, post the custom, guideline-referenced comment you discussed with the user:
       ```bash
       python3 osc_review_helper.py comment <request_id> "your custom comment"
       ```
     - Remember, do **not** accept or decline requests on your own; always gain explicit user agreement first.
