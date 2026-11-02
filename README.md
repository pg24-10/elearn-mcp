# ISB eLearn connector for Claude

A read-only connector that lets Claude see your ISB eLearn (Moodle) courses: deadlines, assignment briefs, announcements, forum threads, grades, groups, quizzes and course files (PDF, Excel, Word, PowerPoint, including watermarked files).

It runs in two ways from the same file:

- **On your laptop** inside the Claude desktop app (`python3 elearn_mcp.py setup`).
- **Hosted online** so it also works in Claude on your phone and on the web, with the laptop off (`python3 elearn_mcp.py serve`, deployed with the Dockerfile).

## Tools

| Tool | What it does |
|---|---|
| `upcoming_deadlines` | What's due in the next N days, group or individual, submitted or not |
| `assignments` | Every assignment with full brief, attached files, status, grade, feedback |
| `forum_status` | Graded or dated forums and whether you've posted |
| `announcements` *(new)* | Recent instructor and TA announcements across courses, full text and files |
| `forum_discussions` *(new)* | Threads in a forum |
| `read_discussion` *(new)* | Every post in a thread, with attachments |
| `grades` *(new)* | Course totals, or every graded item in one course with feedback |
| `my_groups` *(new)* | Your exact group name in each course, plus members and emails |
| `quizzes` *(new)* | Quiz windows, time limits, attempts, best grade |
| `calendar` *(new)* | Everything on your eLearn calendar for the next N days |
| `find_files` *(new)* | Search file and activity names across all courses |
| `recent_files` *(new)* | Files added or changed in the last N days |
| `list_courses`, `course_contents` | Courses, sections, activities and file links |
| `read_file`, `search_file`, `file_outline`, `view_pages` | Read, search, map and see pages of course files |

Fixes in this version: dates always show in India time (cloud servers run on UTC), `assignments` is sorted by due date (it was sorted alphabetically by weekday), and the watermarked-file login can't run twice at once.

## Laptop setup (Claude desktop app)

```
python3 elearn_mcp.py setup
```

It installs what it needs, asks for your eLearn key and connects Claude. Run it again to update. The steps for getting the key are at the top of `elearn_mcp.py`.

## Hosted setup (phone, web and desktop)

### How it works

GitHub stores the code. Google Cloud Run runs it and gives it a permanent https address. Each push to GitHub rebuilds and redeploys it automatically. Claude's servers call that address when you use the connector, so it works from your phone with the laptop off.

The server signs in to eLearn with the same Moodle app key your laptop uses. Your Microsoft password is never stored. Claude has to sign in to the server with a password you choose (`ACCESS_PASSWORD`), so nobody else can use your connector even if they find the address.

Cloud Run's free allowance (2 million requests a month) is far more than one person uses. It needs a billing account (a card) to switch on, but normal use stays at zero.

### Step 1: get your eLearn key from the laptop

```
python3 ~/elearn-mcp/elearn_mcp.py show-key
```

Keep the two values it prints for Step 2. Don't paste them into chats or files.

### Step 2: create the Cloud Run service

1. Go to console.cloud.google.com, sign in, and create a project (for example `elearn-mcp`). Turn on billing if asked.
2. Open **Cloud Run**, click **Deploy container**, then **Service**, and choose **Continuously deploy from a repository (source or function)**.
3. Click **Set up with Cloud Build**, connect GitHub, and pick `pg24-10/elearn-mcp`, branch `^main$`, build type **Dockerfile** (path `/Dockerfile`).
4. Service name `elearn-mcp`. Region `asia-south1 (Mumbai)`.
5. Authentication: **Allow public access**. The connector does its own password login, and Claude can't use Google sign-in.
6. Billing: **Request-based**. Scaling: minimum instances **0**, maximum instances **1**.
7. Under **Containers > Variables & secrets**, add:
   - `ELEARN_TOKEN`: from Step 1
   - `ELEARN_PRIVATE_TOKEN`: from Step 1 (needed for watermarked files)
   - `ACCESS_PASSWORD`: a new password of at least 10 characters, used only for this connector
8. Click **Create** and wait for the first build (a few minutes).

### Step 3: set the public address

Open the service URL Cloud Run shows (like `https://elearn-mcp-xxxx.asia-south1.run.app`). The page tells you the exact `PUBLIC_URL` to set. In Cloud Run click **Edit and deploy new revision**, add the variable `PUBLIC_URL` with that value, and deploy. The page should now say the connector is running.

### Step 4: add it to Claude

Custom connectors are added on claude.ai or in the desktop app, not in the phone app. Once added, they work on the phone too.

1. In Claude's settings, open **Connectors** and add a custom connector.
2. Name: `ISB eLearn`. URL: your `PUBLIC_URL` followed by `/mcp`.
3. Click **Connect**. A page asks for your `ACCESS_PASSWORD`. Enter it and click **Allow access**.
4. Remove the old laptop connector from the desktop app's config (or leave both; the tool names are the same, so one is enough).

### Changing things later

- **New code**: push to `main`. Cloud Run rebuilds automatically.
- **New eLearn key** (if eLearn stops accepting it): update `ELEARN_TOKEN` and `ELEARN_PRIVATE_TOKEN` in Cloud Run and deploy a new revision.
- **Sign out everywhere**: change `ACCESS_PASSWORD`. Claude will ask you to connect again.

## Security notes

- The connector is read-only: it can't submit, post or change anything on eLearn.
- Only Claude's sign-in callback (claude.ai, claude.com) and localhost can receive login codes. Five wrong passwords lock sign-in for 15 minutes.
- Logins and tokens are signed with a key derived from your password and eLearn key, so they survive restarts without a database. Tokens last 8 hours and renew automatically for up to 4 months of inactivity.
- Your eLearn key gives read access to your account. Keep it in Cloud Run's settings only. If it leaks, log out of all sessions on eLearn and make a new one.

## Tests

`python tests/test_hosted.py` runs the hosted server against a fake eLearn (`tests/fake_moodle.py`): the full sign-in flow Claude uses, every new tool, and a restart.
