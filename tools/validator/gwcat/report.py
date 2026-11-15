"""Findings of a validation run: errors (the version / addon is not published), warnings (published, shown in the
report), review flags (published only after a moderator approves, 11 section 5.2) and notes."""

import json

ERROR = "error"
WARNING = "warning"
REVIEW = "review"
NOTE = "note"

_ORDER = {ERROR: 0, REVIEW: 1, WARNING: 2, NOTE: 3}


class Report:
    def __init__(self, subject=""):
        self.subject = subject
        self.items = []  # dicts: level, code, message, path
        self.data = {}  # facts found on the way (manifest, caps, files, delta ...) for the caller

    def add(self, level, code, message, path=None):
        item = {"level": level, "code": code, "message": message}
        if path:
            item["path"] = path
        self.items.append(item)
        return item

    def error(self, code, message, path=None):
        return self.add(ERROR, code, message, path)

    def warning(self, code, message, path=None):
        return self.add(WARNING, code, message, path)

    def review(self, code, message, path=None):
        return self.add(REVIEW, code, message, path)

    def note(self, code, message, path=None):
        return self.add(NOTE, code, message, path)

    def extend(self, other):
        self.items.extend(other.items)
        for k, v in other.data.items():
            self.data.setdefault(k, v)

    def apply_exceptions(self, codes):
        """moderation.ltx [exceptions]: these codes of this addon become notes (a moderator accepted them)"""
        codes = set(codes or ())
        for item in self.items:
            if item["code"] in codes and item["level"] in (ERROR, WARNING, REVIEW):
                item["message"] += " (accepted by a moderator: %s)" % item["level"]
                item["level"] = NOTE

    def of(self, level):
        return [i for i in self.items if i["level"] == level]

    @property
    def errors(self):
        return self.of(ERROR)

    @property
    def ok(self):
        return not self.errors

    def codes(self, level=None):
        return [i["code"] for i in self.items if level is None or i["level"] == level]

    def sorted_items(self):
        return sorted(self.items, key=lambda i: (_ORDER.get(i["level"], 9), i.get("path") or "", i["code"]))

    def to_json(self):
        return {"subject": self.subject, "ok": self.ok,
                "counts": {lvl: len(self.of(lvl)) for lvl in (ERROR, REVIEW, WARNING, NOTE)},
                "items": self.sorted_items(), "data": self.data}

    def to_markdown(self, title=None):
        lines = ["### " + (title or ("Validation: " + self.subject)), ""]
        counts = ", ".join("%s: %d" % (lvl, len(self.of(lvl))) for lvl in (ERROR, REVIEW, WARNING, NOTE))
        lines.append(("**OK**" if self.ok else "**FAILED**") + " - " + counts)
        lines.append("")
        if self.items:
            lines.append("| level | code | where | message |")
            lines.append("|---|---|---|---|")
            for i in self.sorted_items():
                msg = i["message"].replace("|", "\\|").replace("\n", " ")
                lines.append("| %s | `%s` | %s | %s |" % (i["level"], i["code"],
                                                        ("`%s`" % i["path"]) if i.get("path") else "", msg))
        return "\n".join(lines) + "\n"

    def to_text(self):
        out = ["%s: %s" % (self.subject, "OK" if self.ok else "FAILED")]
        for i in self.sorted_items():
            out.append("  %-7s %-28s %s%s" % (i["level"], i["code"], ("[%s] " % i["path"]) if i.get("path") else "",
                                              i["message"]))
        return "\n".join(out)

    def dumps(self):
        return json.dumps(self.to_json(), ensure_ascii=False, indent=2)
