"""
Dispatcharr — Channel Name Cleaner (v0.2.0)

Tidies messy IPTV channel names (pipe/bracket quality tags, dangling separators,
fancy-unicode marks) with a dry-run preview, per-group scope, a protect-list, a
collision strategy (Channel.name is unique), per-rule/group stats, fast bulk Apply,
timestamped multi-step undo, a single-name tester and an optional scheduler.

All actions read their configuration from context["settings"] (see plugin.json).
"""

import csv
import glob
import logging
import os
import re
import threading
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone

from apps.channels.models import Channel
from django.db import transaction

LOGGER = logging.getLogger("plugins.channel_name_cleaner")

PLUGIN_KEY = "channel_name_cleaner"
PLUGIN_NAME = "Channel Name Cleaner"
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(PLUGIN_DIR, "backups")
PREVIEW_CSV = os.path.join(PLUGIN_DIR, "last_preview.csv")
SUMMARY_TXT = os.path.join(PLUGIN_DIR, "last_summary.txt")
SCHEDULER_THREAD = "cnc-scheduler"

# A pipe/bracket segment that is *only* one of these is junk formatting, not a name.
_QUALITY = r"FHD\d*|UHD|FHD|HD|SD|4K|8K|HEVC|H\.?265|H\.?264|RAW|MULTI|MULTI-?AUDIO|VIP|HQ|LQ|LOW|DUAL|GEO|BACKUP"
QUALITY_SEGMENT = re.compile(r"^\s*(?:%s)\s*$" % _QUALITY, re.IGNORECASE)
# Bracket contents that are entirely junk tokens (so '(Moldova)' survives, '(1080p)' / '[VIP]' go).
_BRACKET_JUNK = r"\d{3,4}p|%s" % _QUALITY
BRACKET_JUNK_INNER = re.compile(r"^(?:\s*(?:%s)\s*[/,&+]?\s*)+$" % _BRACKET_JUNK, re.IGNORECASE)

# Superscript / modifier-letter quality marks (not real script letters).
UNICODE_QUALITY = ["ᵁᴴᴰ", "ᴴᴰ", "ᴿᴬᵂ", "ᴴᴱᵛᶜ", "⁴ᴷ", "ᔕᗪ"]

# Real non-Latin scripts — a name containing these is a genuine foreign channel; leave it alone.
FOREIGN_SCRIPT = re.compile(
    r"[Ͱ-ϿЀ-ӿ֐-׿؀-ۿ"
    r"ऀ-ॿ぀-ヿ㐀-鿿가-힯]"
)

DEFAULT_PROTECTED_GROUPS = "Todays Live Events, Info | Service Status"


def _truthy(settings, key, default):
    v = settings.get(key, default)
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v) if v is not None else default


def _csv_set(value):
    if not value:
        return set()
    return {p.strip().casefold() for p in str(value).split(",") if p.strip()}


def _parse_custom_rules(value):
    rules = []
    if not value:
        return rules
    for line in str(value).splitlines():
        line = line.strip()
        if not line or "=>" not in line:
            continue
        pat, _, rep = line.partition("=>")
        try:
            rules.append((re.compile(pat.strip()), rep.strip()))
        except re.error:
            LOGGER.warning("%s: ignoring invalid custom rule: %s", PLUGIN_NAME, line)
    return rules


def _smart_title(name):
    out = []
    for word in name.split(" "):
        if not word:
            continue
        if word.isupper() or any(c.isdigit() for c in word):
            out.append(word)
        else:
            out.append(word[:1].upper() + word[1:].lower())
    return " ".join(out)


def _strip_bracketed_junk(s):
    def repl(m):
        return " " if BRACKET_JUNK_INNER.match(m.group(1)) else m.group(0)
    s = re.sub(r"\(([^()]*)\)", repl, s)
    s = re.sub(r"\[([^\[\]]*)\]", repl, s)
    return s


class Plugin:
    name = PLUGIN_NAME
    version = "0.2.0"
    description = "Clean up messy IPTV channel names — preview, protect-list, collision strategy, stats, fast bulk apply, multi-undo and an optional scheduler."
    author = "noodlemctwoodle"
    help_url = "https://github.com/noodlemctwoodle/dispatcharr-channel-name-cleaner"

    def __init__(self):
        self._start_scheduler()

    # ---- rule config ---------------------------------------------------

    def _rules(self, settings):
        sep = settings.get("join_separator", " ")
        if sep is None or sep == "":
            sep = " "
        return {
            "strip_unicode_quality": _truthy(settings, "strip_unicode_quality", True),
            "unicode_nfkc": _truthy(settings, "unicode_nfkc", False),
            "strip_bracketed_junk": _truthy(settings, "strip_bracketed_junk", True),
            "strip_pipe_junk": _truthy(settings, "strip_pipe_junk", True),
            "normalize_plus_one": _truthy(settings, "normalize_plus_one", False),
            "fix_casing": _truthy(settings, "fix_casing", False),
            "normalize_whitespace": _truthy(settings, "normalize_whitespace", True),
            "join_separator": str(sep),
        }

    def _is_protected(self, name, group_name, settings, protect_re):
        if group_name and group_name.casefold() in _csv_set(
            settings.get("protected_groups") or DEFAULT_PROTECTED_GROUPS
        ):
            return True
        if _truthy(settings, "protect_foreign_scripts", True) and FOREIGN_SCRIPT.search(name):
            return True
        if _truthy(settings, "protect_leading_symbol", True) and name.strip().startswith("&"):
            return True
        if protect_re and protect_re.search(name):
            return True
        return False

    def _clean(self, name, rules, custom):
        """Return (new_name, tags:set, removed_quality:list)."""
        s = name or ""
        tags = set()
        removed = []

        def step(tag, new):
            nonlocal s
            if new != s:
                tags.add(tag)
                s = new

        if rules["strip_unicode_quality"]:
            tmp = s
            for mark in UNICODE_QUALITY:
                tmp = tmp.replace(mark, " ")
            step("unicode", tmp)
        if rules["unicode_nfkc"]:
            step("nfkc", unicodedata.normalize("NFKC", s))
        if rules["strip_bracketed_junk"]:
            step("bracket", _strip_bracketed_junk(s))
        if rules["strip_pipe_junk"] and "|" in s:
            kept = []
            for part in (p.strip() for p in s.split("|")):
                if not part:
                    continue
                if QUALITY_SEGMENT.match(part):
                    removed.append(part.upper())
                    continue
                kept.append(part)
            step("pipe", rules["join_separator"].join(kept))
        if rules["normalize_plus_one"]:
            step("plus_one", re.sub(r"(?<=[A-Za-z0-9])\+1\b", " +1", s))
        for pat, rep in custom:
            try:
                step("custom", pat.sub(rep, s))
            except re.error:
                pass
        if rules["fix_casing"]:
            step("casing", _smart_title(s))
        if rules["normalize_whitespace"]:
            step("whitespace", re.sub(r"\s{2,}", " ", s).strip())
        return s.strip(), tags, removed

    # ---- change computation + collision resolution ---------------------

    def _compute_changes(self, settings):
        include = _csv_set(settings.get("selected_groups"))
        exclude = _csv_set(settings.get("ignore_groups"))
        rules = self._rules(settings)
        custom = _parse_custom_rules(settings.get("custom_rules"))
        strategy = (settings.get("collision_strategy") or "suffix").strip().lower()
        sep = rules["join_separator"]
        protect_re = None
        pr = (settings.get("protect_regex") or "").strip()
        if pr:
            try:
                protect_re = re.compile(pr)
            except re.error:
                LOGGER.warning("%s: invalid protect_regex ignored: %s", PLUGIN_NAME, pr)

        rows = []
        for ch in Channel.objects.select_related("channel_group").iterator():
            group_name = ch.channel_group.name if ch.channel_group_id and ch.channel_group else ""
            if include and group_name.casefold() not in include:
                continue
            if group_name.casefold() in exclude:
                continue
            old = ch.name or ""
            if self._is_protected(old, group_name, settings, protect_re):
                rows.append({"id": ch.id, "group": group_name, "old": old, "new": old,
                             "status": "protected", "tags": set(), "rq": []})
                continue
            new, tags, removed = self._clean(old, rules, custom)
            if not new or new == old:
                rows.append({"id": ch.id, "group": group_name, "old": old, "new": new or old,
                             "status": "nochange", "tags": tags, "rq": removed})
                continue
            rows.append({"id": ch.id, "group": group_name, "old": old, "new": new,
                         "status": "change", "tags": tags, "rq": removed})

        self._resolve_collisions(rows, strategy, sep)
        counts = Counter(r["status"] for r in rows)
        return rows, counts

    def _resolve_collisions(self, rows, strategy, sep):
        changing = [r for r in rows if r["status"] == "change"]
        existing_cf = {n.casefold() for n in Channel.objects.values_list("name", flat=True)}
        vacated = {r["old"].casefold() for r in changing}
        base_counts = Counter(r["new"].casefold() for r in changing)
        taken = set(existing_cf) - vacated

        # Pass 1: assign the clearly-unique ones (single claimant, not blocked by a kept name).
        deferred = []
        for r in sorted(changing, key=lambda x: x["id"]):
            key = r["new"].casefold()
            if base_counts[key] == 1 and key not in taken:
                taken.add(key)
            else:
                deferred.append(r)

        # Pass 2: resolve the collisions per strategy.
        for r in sorted(deferred, key=lambda x: x["id"]):
            base = r["new"]
            if strategy == "skip":
                r["status"] = "collision"
                continue
            cand = None
            if strategy != "numeric" and r["rq"]:
                c = (base + sep + r["rq"][0].upper()).strip()
                if c.casefold() not in taken:
                    cand = c
            if cand is None:
                i = 2
                while ("%s (%d)" % (base, i)).casefold() in taken:
                    i += 1
                cand = "%s (%d)" % (base, i)
            taken.add(cand.casefold())
            r["new"] = cand  # stays status "change"

    # ---- CSV + summary -------------------------------------------------

    def _write_csv(self, path, rows):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["id", "group", "status", "old_name", "new_name", "rules"])
                for r in rows:
                    w.writerow([r["id"], r["group"], r["status"], r["old"], r["new"],
                                "|".join(sorted(r.get("tags", [])))])
            return True
        except OSError as e:
            LOGGER.error("%s: could not write %s: %s", PLUGIN_NAME, path, e)
            return False

    def _summary(self, title, counts, rows, settings, extra=""):
        n = int(settings.get("max_preview_rows", 400) or 400)
        changes = [r for r in rows if r["status"] == "change"]
        rule_counts = Counter()
        group_counts = Counter()
        for r in changes:
            for t in r["tags"]:
                rule_counts[t] += 1
            group_counts[r["group"] or "(no group)"] += 1

        lines = [title, ""]
        lines.append("Will change: %d   Collisions (skipped): %d   Protected: %d   No change: %d"
                     % (counts.get("change", 0), counts.get("collision", 0),
                        counts.get("protected", 0), counts.get("nochange", 0)))
        if extra:
            lines.append(extra)
        if rule_counts:
            lines.append("")
            lines.append("By rule: " + ", ".join("%s=%d" % (k, v) for k, v in rule_counts.most_common()))
        if group_counts:
            lines.append("Top groups: " + ", ".join("%s (%d)" % (k, v) for k, v in group_counts.most_common(8)))
        if changes:
            lines.append("")
            lines.append("Examples (old  ->  new):")
            for r in changes[:n]:
                lines.append("  %s  ->  %s" % (r["old"], r["new"]))
        collisions = [r for r in rows if r["status"] == "collision"][:40]
        if collisions:
            lines.append("")
            lines.append("Skipped collisions:")
            for r in collisions:
                lines.append("  %s  ->  %s  (skipped)" % (r["old"], r["new"]))
        text = "\n".join(lines)
        try:
            with open(SUMMARY_TXT, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            pass
        return text

    # ---- actions -------------------------------------------------------

    def _preview(self, settings, logger):
        rows, counts = self._compute_changes(settings)
        self._write_csv(PREVIEW_CSV, rows)
        logger.info("%s: preview — %d changes, %d collisions", PLUGIN_NAME,
                    counts.get("change", 0), counts.get("collision", 0))
        return {"status": "success", "message": self._summary(
            "PREVIEW (dry run) — nothing was changed.", counts, rows, settings,
            extra="Full list written to last_preview.csv in the plugin folder.")}

    def _apply(self, settings, logger):
        rows, counts = self._compute_changes(settings)
        changes = [r for r in rows if r["status"] == "change"]
        if not changes:
            return {"status": "success",
                    "message": self._summary("APPLY — nothing to change.", counts, rows, settings)}
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(BACKUP_DIR, "apply_%s.csv" % ts)
        if not self._write_csv(backup, changes):
            return {"status": "error", "message": "Could not write the backup file; aborting."}

        applied, failed = 0, 0
        for i in range(0, len(changes), 500):
            chunk = changes[i:i + 500]
            try:
                with transaction.atomic():
                    Channel.objects.bulk_update(
                        [Channel(id=r["id"], name=r["new"]) for r in chunk], ["name"], batch_size=500)
                applied += len(chunk)
            except Exception as e:  # transient unique clash in a chunk -> per-row fallback
                logger.warning("%s: bulk chunk fell back to per-row (%s)", PLUGIN_NAME, e)
                for r in chunk:
                    try:
                        with transaction.atomic():
                            ch = Channel.objects.select_for_update().get(id=r["id"])
                            ch.name = r["new"]
                            ch.save(update_fields=["name"])
                        applied += 1
                    except Exception as e2:
                        failed += 1
                        logger.warning("%s: rename failed id=%s: %s", PLUGIN_NAME, r["id"], e2)
        logger.info("%s: applied %d renames (%d failed)", PLUGIN_NAME, applied, failed)
        return {"status": "success", "message": self._summary(
            "APPLY complete.", counts, rows, settings,
            extra="Renamed: %d   Failed: %d. Backup: %s — use 'Undo Last' to revert."
                  % (applied, failed, os.path.basename(backup)))}

    def _undo(self, settings, logger):
        backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "apply_*.csv")), reverse=True)
        if not backups:
            return {"status": "error", "message": "No backup found — run Apply first."}
        path = backups[0]
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        except OSError as e:
            return {"status": "error", "message": "Could not read backup: %s" % e}
        restored, skipped = 0, 0
        for row in rows:
            try:
                with transaction.atomic():
                    ch = Channel.objects.select_for_update().get(id=int(row["id"]))
                    if (ch.name or "") == row["new_name"]:
                        ch.name = row["old_name"]
                        ch.save(update_fields=["name"])
                        restored += 1
                    else:
                        skipped += 1
            except Exception as e:
                skipped += 1
                logger.warning("%s: undo failed id=%s: %s", PLUGIN_NAME, row.get("id"), e)
        try:
            os.rename(path, path + ".undone")  # peel back one step; next undo takes the prior apply
        except OSError:
            pass
        return {"status": "success",
                "message": "Undo of %s complete. Restored: %d   Skipped (changed since): %d."
                           % (os.path.basename(path), restored, skipped)}

    def _test_name(self, settings, logger):
        name = (settings.get("test_name") or "").strip()
        if not name:
            return {"status": "error", "message": "Enter a name in 'Test a Name' first."}
        rules = self._rules(settings)
        custom = _parse_custom_rules(settings.get("custom_rules"))
        protect_re = None
        pr = (settings.get("protect_regex") or "").strip()
        if pr:
            try:
                protect_re = re.compile(pr)
            except re.error:
                pass
        if self._is_protected(name, "", settings, protect_re):
            return {"status": "success", "message": "PROTECTED — '%s' would be left unchanged." % name}
        new, tags, removed = self._clean(name, rules, custom)
        rules_txt = ", ".join(sorted(tags)) if tags else "none"
        rq = (" | removed quality: " + ", ".join(removed)) if removed else ""
        return {"status": "success",
                "message": "%s\n  ->  %s\nRules fired: %s%s" % (name, new or name, rules_txt, rq)}

    def _status(self, settings, logger):
        if os.path.exists(SUMMARY_TXT):
            try:
                with open(SUMMARY_TXT, encoding="utf-8") as fh:
                    return {"status": "success", "message": fh.read()}
            except OSError:
                pass
        return {"status": "success", "message": "No previous run yet. Start with Preview."}

    def run(self, action, params, context):
        context = context or {}
        settings = context.get("settings", {}) or {}
        logger = context.get("logger", LOGGER)
        try:
            handler = {
                "preview": self._preview,
                "apply": self._apply,
                "undo_last": self._undo,
                "test_name": self._test_name,
                "status": self._status,
            }.get(action)
            if handler is None:
                return {"status": "error", "message": "Unknown action: %s" % action}
            return handler(settings, logger)
        except Exception as e:
            logger.exception("%s: run(%s) failed", PLUGIN_NAME, action)
            return {"status": "error", "message": "%s: %s" % (type(e).__name__, e)}

    # ---- scheduler (opt-in, reads its own persisted settings) ----------

    def _start_scheduler(self):
        if any(t.name == SCHEDULER_THREAD for t in threading.enumerate()):
            return  # already running in this process
        t = threading.Thread(target=self._scheduler_loop, name=SCHEDULER_THREAD, daemon=True)
        t.start()

    def _scheduler_loop(self):
        last_run = 0.0
        while True:
            time.sleep(300)  # check every 5 minutes
            try:
                from apps.plugins.models import PluginConfig
                cfg = PluginConfig.objects.filter(key=PLUGIN_KEY).first()
                if not cfg or not cfg.enabled:
                    continue
                settings = cfg.settings or {}
                if not _truthy(settings, "enable_schedule", False):
                    continue
                hours = float(settings.get("schedule_hours", 12) or 12)
                now = time.time()
                if now - last_run < hours * 3600:
                    continue
                last_run = now
                LOGGER.info("%s: scheduled run starting", PLUGIN_NAME)
                self._apply(settings, LOGGER)
            except Exception:
                LOGGER.exception("%s: scheduler cycle failed", PLUGIN_NAME)
