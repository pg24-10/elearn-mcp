"""A tiny stand-in for ISB eLearn's Moodle web service, for offline tests.

Run:  python tests/fake_moodle.py 8099
Then point the connector at it with ELEARN_URL=http://127.0.0.1:8099
"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

NOW = int(time.time())
DAY = 86400
TOKEN = "testtoken0123456789abcdef01234567"
calls: list[str] = []

COURSES = [{"id": 11056, "fullname": "MKTS_PGPMOH_2026-27_T5", "shortname": "MKTS"},
           {"id": 11057, "fullname": "STAR_PGPMOH_2026-27_T5", "shortname": "STAR"}]


def respond(fn: str, p: dict):
    if fn == "core_webservice_get_site_info":
        return {"userid": 42, "fullname": "Pranav Gupta", "username": "pranav",
                "functions": [{"name": "mod_forum_get_forum_discussions"}]}
    if fn == "core_enrol_get_users_courses":
        return COURSES
    if fn == "mod_forum_get_forums_by_courses":
        return [{"id": 58963, "course": 11056, "type": "news", "name": "Announcements", "cmid": 900},
                {"id": 58964, "course": 11056, "type": "general", "name": "Case forum", "cmid": 901,
                 "duedate": NOW + 3 * DAY, "grade_forum": 10, "numdiscussions": 2, "intro": "<p>Post your view</p>"},
                {"id": 58970, "course": 11057, "type": "news", "name": "Announcements", "cmid": 910}]
    if fn == "mod_forum_get_forum_discussions":
        if p["forumid"] == "58963":
            return {"discussions": [
                {"discussion": 75413, "subject": "Submit choice of project", "created": NOW - 3600,
                 "timemodified": NOW - 3600, "userfullname": "Vrinda Sharma1", "numreplies": 0,
                 "message": "<p>Choose <strong>Inorganic</strong> or <strong>JSW</strong>.</p><ul><li>One member per group</li></ul>",
                 "attachments": []},
                {"discussion": 75273, "subject": "Group project", "created": NOW - 6 * DAY,
                 "timemodified": NOW - 6 * DAY, "userfullname": "Vrinda Sharma1", "numreplies": 1,
                 "message": "<p>Details uploaded on LMS.</p>",
                 "attachments": [{"filename": "MKTS Group Project.pdf",
                                  "fileurl": "http://127.0.0.1:8099/webservice/pluginfile.php/1/mod_forum/attachment/1/MKTS%20Group%20Project.pdf"}]},
                {"discussion": 70000, "subject": "Old notice", "created": NOW - 60 * DAY,
                 "userfullname": "TA", "message": "old"}]}
        return {"discussions": []}
    if fn == "mod_forum_get_discussion_posts":
        return {"posts": [
            {"id": 1, "parentid": 0, "subject": "Group project", "timecreated": NOW - 6 * DAY,
             "author": {"fullname": "Vrinda Sharma1"}, "message": "<p>Details uploaded.</p>", "attachments": []},
            {"id": 2, "parentid": 1, "subject": "Re: Group project", "timecreated": NOW - 5 * DAY,
             "author": {"fullname": "A Student"}, "message": "<p>Is the deadline firm?</p>",
             "attachments": [{"filename": "q.png", "url": "http://127.0.0.1:8099/x/q.png"}]}]}
    if fn == "gradereport_overview_get_course_grades":
        return {"grades": [{"courseid": 11056, "grade": "B+", "rank": None}]}
    if fn == "gradereport_user_get_grade_items":
        return {"usergrades": [{"gradeitems": [
            {"itemname": "Quiz 1", "itemtype": "mod", "itemmodule": "quiz", "gradeformatted": "8.00",
             "rangeformatted": "0-10", "percentageformatted": "80.00 %", "weightformatted": "10.00 %",
             "feedback": "<p>Good work</p>"},
            {"itemname": None, "itemtype": "course", "gradeformatted": "B+", "rangeformatted": "0-100"}]}]}
    if fn == "core_group_get_course_user_groups":
        return {"groups": [{"id": 5, "name": "MKTS-Section B-Group 7", "courseid": 11056}]}
    if fn == "core_enrol_get_enrolled_users":
        assert p.get("options[0][name]") == "groupid", p
        return [{"fullname": "Pranav Gupta", "email": "p@isb.edu"}, {"fullname": "Ritika Asnani", "email": "r@isb.edu"}]
    if fn == "mod_quiz_get_quizzes_by_courses":
        return {"quizzes": [{"id": 3, "course": 11057, "name": "Pre-read quiz", "timeopen": NOW,
                             "timeclose": NOW + 2 * DAY, "timelimit": 900, "attempts": 1, "grade": 10.0,
                             "intro": "<p>Covers the case</p>"}]}
    if fn == "mod_quiz_get_user_attempts":
        return {"attempts": [{"id": 1}]}
    if fn == "mod_quiz_get_user_best_grade":
        return {"hasgrade": True, "grade": 7.5}
    if fn == "core_calendar_get_calendar_events":
        assert p.get("events[courseids][0]") == "11056", p
        assert p.get("options[userevents]") == "1", p
        return {"events": [{"name": "MKTS project due", "courseid": 11056, "modulename": "assign",
                            "eventtype": "due", "timestart": NOW + 2 * DAY, "timeduration": 0},
                           {"name": "Team meeting", "courseid": 0, "eventtype": "user",
                            "timestart": NOW + DAY, "timeduration": 3600, "location": "AC4 LT"}]}
    if fn == "core_course_get_contents":
        if p["courseid"] == "11056":
            return [{"name": "Group project", "summary": "", "modules": [
                {"name": "MKTS Group Project Brief", "modname": "resource", "instance": 77,
                 "url": "http://127.0.0.1:8099/mod/resource/view.php?id=1",
                 "contents": [{"type": "file", "filename": "MKTS Group Project.pdf",
                               "fileurl": "http://127.0.0.1:8099/webservice/pluginfile.php/1/x/MKTS%20Group%20Project.pdf",
                               "timecreated": NOW - 6 * DAY, "timemodified": NOW - 6 * DAY}]},
                {"name": "JSW case (watermarked)", "modname": "filewithwatermark",
                 "url": "http://127.0.0.1:8099/mod/filewithwatermark/view.php?id=2", "contents": []},
                {"name": "Announcements", "modname": "forum", "instance": 58963,
                 "url": "http://127.0.0.1:8099/mod/forum/view.php?id=900"},
                {"name": "Reading list", "modname": "url", "url": "http://127.0.0.1:8099/mod/url/view.php?id=3",
                 "contents": [{"type": "url", "fileurl": "https://hbr.org/some-article"}]}]}]
        return [{"name": "Week 1", "modules": [
            {"name": "Session 1 slides", "modname": "resource",
             "contents": [{"type": "file", "filename": "s1.pptx", "fileurl": "http://127.0.0.1:8099/f/s1.pptx",
                           "timecreated": NOW - 40 * DAY, "timemodified": NOW - 40 * DAY}]}]}]
    if fn == "mod_assign_get_assignments":
        return {"courses": [{"fullname": "MKTS_PGPMOH_2026-27_T5", "assignments": [
            {"id": 1, "name": "Later one", "duedate": NOW + 20 * DAY, "teamsubmission": 1, "intro": "<p>B</p>"},
            {"id": 2, "name": "Sooner one", "duedate": NOW + 1 * DAY, "teamsubmission": 0, "intro": "<p>A</p>"}]}]}
    if fn == "mod_assign_get_submission_status":
        return {"lastattempt": {"submission": {"status": "new"}}}
    return {"exception": "invalid_parameter_exception", "errorcode": "invalidrecord",
            "message": f"fake moodle has no {fn}"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0))).decode()
        p = {k: v[0] for k, v in parse_qs(body).items()}
        fn = p.get("wsfunction", "")
        calls.append(fn)
        if p.get("wstoken") != TOKEN:
            out = {"exception": "moodle_exception", "errorcode": "invalidtoken", "message": "Invalid token"}
        else:
            try:
                out = respond(fn, p)
            except AssertionError as e:
                out = {"exception": "assert", "errorcode": "assert", "message": f"bad params: {e}"}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8099), H).serve_forever()
