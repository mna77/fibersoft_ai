# Fibersoft AI  (v1.1.0)

Context-aware AI assistant for **Fibersoft ERP** (Frappe v15), powered by the Claude API.

The black & white Fibersoft logo sits in the corner of every desk screen. Clicking it opens a chat panel. The assistant knows which form, list, report or workspace the user is looking at. It reads the exact records and reports it needs, using the logged-in user's own permissions. It can also prepare changes, which the user confirms before anything happens.

---

## Features

**Understands the screen**
- **Form:** the document's values, including child rows, unsaved changes and form messages.
- **List and Report view:** the filters, the sort and the visible rows.
- **Script/Query report:** the report name, the filters on screen, the columns and rows, and the report summary.
- **Workspace or dashboard:** the number cards, chart titles and visible text (including Custom HTML Block dashboards).
- **Last error:** the most recent error message the user saw ("why can't I submit this?").
- **Selected text:** any text the user highlighted on the page.
- A context chip lets the user switch screen sharing off.

**Accurate answers (tools the AI uses)**
| Tool | Purpose |
|---|---|
| `describe_doctype` | Fields, child tables, permissions, workflow, reports built on the DocType |
| `search_doctypes` | Find DocTypes / reports / fields (incl. custom fields) by keyword |
| `find_records` | Resolve "Ali Traders" → exact record name |
| `get_document` | Full document incl. child rows (permlevel-aware) |
| `list_documents` / `count_documents` | Filtered lists with exact total counts |
| `aggregate` | Database-computed sum/count/avg/min/max, group-by, day/week/month/quarter/year buckets. Child-table filters never double-count |
| `run_report` / `get_report_info` | Runs any Query/Script/Report Builder report (incl. your custom reports) with server-side filter/sort/limit and exact column totals |
| `get_document_activity` | Who changed what and when (versions, comments, workflow) |
| `get_balance` | Exact GL balance for party/account (group accounts roll up), opening/period/closing |
| `get_stock_levels` | Bin quantities and value by item/warehouse (warehouse groups roll up) |
| `calculate` | Exact decimal arithmetic, so the model never does mental math |

The AI is told never to state a figure that didn't come from the screen or a tool. Cancelled documents are excluded by default, and every answer states the scope it used (period, company, filters).

**Actions with confirmation:** create, update (incl. child rows), submit, cancel, workflow action (Approve/Reject...), comment, assign.
- The AI only *proposes*. The user sees an action card with a preview and clicks **Confirm**.
- Each proposal is **dry-run validated** in a rolled-back transaction. The document's real validations run, so the AI can fix errors before it asks the user to confirm. Totals the system calculates are shown in the preview.
- **Undo** is available for create (while still a draft), update, comment and assign.
- Before executing, the app re-checks permissions and refuses if the document was modified after the action was proposed.

**Chat experience**
- Answers stream live as they are written, with a progress line ("Running report Sales Summary 360…").
- Markdown tables (with Copy / CSV buttons), charts (rendered with Frappe Charts) and clickable record links. Links open the record without closing the chat.
- Chat history (search, delete), new chat, an expand mode, and a mobile full-screen layout.
- Voice input (Chrome / Edge). Answers come back in English, Urdu or Roman Urdu, matching how the user wrote.
- Thumbs up/down feedback, and a Stop button.
- Keyboard: **Ctrl+Shift+K** opens/closes the chat, Enter sends, Shift+Enter adds a new line.

**Token allocation per user (Fibersoft AI Token Limit)**
- Allocate **daily / weekly / monthly tokens**, daily questions and a monthly cost cap to each **User**, each **Role** (with priority), or **Everyone**.
- Which rule applies: User rule → the Role rule with the highest Priority → the Everyone rule → unlimited.
- Optional **Model Override** per rule. For example, Haiku for light users and Opus for managers.
- Counted tokens = input + output + cache writes + 10% of cached reads. This follows real cost, because cached reads are billed at one tenth.
- The user sees a live meter in the chat ("Today: 12,400 of 20,000 tokens") with the reset time. When an allocation runs out mid-answer, the assistant stops looking up data and answers with what it has.
- Deleting a chat hides it from the user but does not reset their usage.
- The Token Limit form shows live usage bars for everyone the rule covers, and has **Copy limits to users…** for bulk setup.
- **Fibersoft AI Usage** report: tokens, questions, % of allocation, average tokens per question, cost, ratings and actions per user. It includes a chart and summary cards.
- **Fibersoft AI** workspace with shortcuts to Settings, Token Limits, the Usage Report, Conversations and the Action Log.

**Administration (Fibersoft AI Settings)**
- API key (stored encrypted), model, token and step limits, prompt caching.
- Allowed roles, blocked DocTypes, redacted fields.
- Action rules per DocType (which action types are allowed).
- Business context and knowledge entries. An entry can apply everywhere or only to one DocType or report.
- Daily question limit per user and a monthly budget cap (USD).
- Usage dashboard: questions, cost, active users, answer rating, actions, top users.
- Full audit trail: **Fibersoft AI Conversation**, **Fibersoft AI Message** (incl. tool trace and token cost) and **Fibersoft AI Action**.

## Security model

1. **Frappe permissions:** every read and action runs as the logged-in user. Role permissions, user permissions and permlevels all apply.
2. **Credentials are never read:** password fields and credential-like fields (API keys, secrets, tokens) are always stripped.
3. **Blocked areas:** the assistant's own data, OAuth/token tables and access logs are hard-blocked. Admins can block more DocTypes and redact individual fields.
4. **System DocTypes are read-only:** User, Role, DocType, Custom Field, Server Script and similar can never be changed by the assistant, even if someone adds an action rule for them.
5. **Actions need confirmation:** actions run only after the same user confirms, only while still pending, and they expire (24 hours by default).
6. **Prompt-injection protection:** text inside records is treated as data, never as instructions.
7. **Users see only their own chats.** System Managers can audit all chats.

---

## Installation (VPS, Frappe v15)

```bash
# 1. put the app in your bench
cd ~/frappe-bench/apps
unzip ~/fibersoft_ai.zip            # creates ~/frappe-bench/apps/fibersoft_ai
#   (or, if you push it to a private Git repo:  bench get-app git@github.com:you/fibersoft_ai.git)

# 2. install the python package and register the app
cd ~/frappe-bench
./env/bin/pip install -e apps/fibersoft_ai
grep -qx fibersoft_ai sites/apps.txt || printf '\nfibersoft_ai\n' >> sites/apps.txt
sed -i '/^$/d' sites/apps.txt       # remove blank lines

# 3. install on your site, build assets, restart
bench --site YOUR_SITE install-app fibersoft_ai
bench build --app fibersoft_ai
bench --site YOUR_SITE clear-cache
sudo supervisorctl restart all      # or: bench restart
```

Background workers must be running for answers to stream (they always are in a standard production setup). If they are not, untick **Run in Background Worker** in settings.

### Configure

1. Open **Fibersoft AI Settings** (search bar → "Fibersoft AI Settings").
2. Paste your **Anthropic API Key**. Keep model `claude-sonnet-5-5`, or choose `claude-opus-5-5` (most capable) or `claude-haiku-5-5` (cheapest). Save.
3. Click **Test Connection**.
4. Fill **Company & Business Context**: industry, currency, fiscal year, naming conventions and what your custom reports show (Sales Summary 360, Receivable Current Ageing, COD Sales 360…). This makes answers much better.
5. Optional: set **Allowed Roles**, **Blocked DocTypes** and **Redacted Fields**. For example, block Salary Slip for everyone except HR by adding allowed roles, or redact salary fields.
6. Optional: tick **Allow Actions** and add **Action Rules** (e.g. ToDo: Create, Comment, Assign; Sales Order: Create, Update; Stock Entry: Workflow).
7. Set **Input/Output Cost** to your model's current prices (Anthropic pricing page), and a **Monthly Budget** if wanted.
8. Tick **Enable Assistant** → Save. Users see the icon after a page reload.
9. Allocate tokens: **Fibersoft AI Token Limit** → New. A practical start:
   - an **Everyone** rule: 20,000 daily / 300,000 monthly;
   - a **Role** rule for your managers' role: 100,000 daily, priority 10;
   - **User** rules for exceptions.
   Then watch the **Fibersoft AI Usage** report for a week and adjust.

### Run the tests (on a TEST site only)

The tests create sample customers, items, invoices and ToDos. Never run them on your live site.

```bash
bench --site test_site set-config allow_tests true
bench --site test_site run-tests --app fibersoft_ai
```

### Upgrade

Replace the folder with the new version, then:

```bash
bench --site YOUR_SITE migrate && bench build --app fibersoft_ai && sudo supervisorctl restart all
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Token limit reached" | The user's allocation is used up. Raise it in their Token Limit rule, or wait for the reset shown in the message. |
| No icon | Settings → Enable Assistant ticked? Is the user's role allowed? Is the user a System User? Hard-refresh the browser (Ctrl+Shift+R). |
| "Thinking…" never finishes | Workers not running: `sudo supervisorctl status`. Or untick *Run in Background Worker*. |
| Answer appears all at once instead of streaming | Socket.io is not reachable. Answers still arrive by polling; check the nginx `/socket.io` block. |
| "API key is invalid" | Re-enter the key, Save, Test Connection. |
| "Model not found" | Check the model name in settings. |
| Error details | Desk → Error Log (titles start with "Fibersoft AI"). |
