"""InvenTree plugin: import SMT pick-and-place feeder stock from the machine's .fig file.

Reads config_feed.fig from an SMB share, parses the feeder table, resolves each feeder's
part_value to an InvenTree part (by name / MPN / IPN), and reconciles that part's stock at a
dedicated 'SMT Feeders' location to the feeder's count. Unmatched feeder values are reported.
Runs on a schedule; also triggerable at /plugin/smallsmt-stock/run for testing.
"""
import logging
import os
import tempfile

from django.http import JsonResponse
from django.urls import path
from django.utils.translation import gettext_lazy as _

from . import SMALLSMT_STOCK_VERSION
from . import smt_parser
from .resolve import build_lookups, resolve, reconcile_stock, nearest

from plugin import InvenTreePlugin
from plugin.mixins import SettingsMixin, ScheduleMixin, UrlsMixin

logger = logging.getLogger("inventree")


class SmallSMTStockPlugin(SettingsMixin, ScheduleMixin, UrlsMixin, InvenTreePlugin):
    """Sync pick-and-place feeder quantities into InvenTree stock."""

    AUTHOR = "Jan Wolf"
    DESCRIPTION = "Import pick-and-place feeder stock from the SMT machine's .fig file into InvenTree."
    VERSION = SMALLSMT_STOCK_VERSION
    MIN_VERSION = "0.16.0"

    NAME = "SmallSMT Stock Import"
    SLUG = "smallsmt-stock"
    TITLE = "SmallSMT Feeder Stock Import"

    SETTINGS = {
        "IMPORT_ENABLED": {
            "name": _("Enable import"), "description": _("Run the scheduled SMT stock import"),
            "default": True, "validator": bool,
        },
        "STOCK_LOCATION": {
            "name": _("Stock location"),
            "description": _("InvenTree stock location that mirrors the SMT feeders (created if missing)"),
            "default": "SMT Feeders",
        },
        "FIG_PATH": {
            "name": _("Feed config path"),
            "description": _("Path on the SMB share to config_feed.fig"),
            "default": "config_feed.fig",
        },
        "UNMATCHED_REPORT_PATH": {
            "name": _("Unmatched report path"),
            "description": _("Path on the SMB share for the human-readable unmatched-feeder report (typo candidates)"),
            "default": "smt_unmatched.txt",
        },
        # --- SMB source ---
        "SMB_HOST": {"name": _("SMB host"), "description": _("SMB/CIFS server host or IP"), "default": ""},
        "SMB_SHARE": {"name": _("SMB share"), "description": _("Share name"), "default": ""},
        "SMB_USER": {"name": _("SMB user"), "default": ""},
        "SMB_PASSWORD": {"name": _("SMB password"), "default": "", "protected": True},
        "SMB_DOMAIN": {"name": _("SMB domain"), "default": ""},
    }

    SCHEDULED_TASKS = {
        "import_smt_stock": {"func": "import_smt_stock", "schedule": "I", "minutes": 60},
    }

    # -------------------------------------------------------------------------------
    def _location(self):
        """Resolve STOCK_LOCATION as a '/'-separated path, creating nested sub-locations.

        e.g. 'Büro Stuttgart/SMT Import Test' -> sub-location 'SMT Import Test' under the
        top-level 'Büro Stuttgart' (each segment created only if missing).
        """
        from stock.models import StockLocation
        pathstr = self.get_setting("STOCK_LOCATION") or "SMT Feeders"
        parent, loc = None, None
        for name in [s.strip() for s in pathstr.split("/") if s.strip()]:
            loc, _created = StockLocation.objects.get_or_create(name=name, parent=parent)
            parent = loc
        return loc

    def _read_fig(self):
        from .smb import read_bytes
        return read_bytes(
            self.get_setting("SMB_HOST"), self.get_setting("SMB_SHARE"), self.get_setting("FIG_PATH"),
            self.get_setting("SMB_USER"), self.get_setting("SMB_PASSWORD"), self.get_setting("SMB_DOMAIN"),
        )

    def run_import(self):
        """Do one import pass; returns a summary dict."""
        raw = self._read_fig()
        tf = tempfile.NamedTemporaryFile(suffix=".fig", delete=False)
        try:
            tf.write(raw); tf.close()
            data = smt_parser.parse_feed(tf.name)
        finally:
            os.unlink(tf.name)

        location = self._location()
        lookups = build_lookups()
        stats = {"matched": 0, "created": 0, "updated": 0, "unchanged": 0, "unmatched": []}
        for group in data["groups"]:
            for comp in group["components"]:
                count = (comp.get("count_number") or "0").strip()
                value = (comp.get("part_value") or "").strip()
                if count in ("", "0") or not value:
                    continue
                part = resolve(value, lookups)
                if part is None:
                    stats["unmatched"].append({"value": value, "count": count})
                    continue
                stats["matched"] += 1
                stats[reconcile_stock(part, count, location)] += 1
        self._report_unmatched(stats["unmatched"], lookups)
        return stats

    def _report_unmatched(self, unmatched, lookups):
        """Log unmatched feeder values with a fuzzy 'nearest match' typo hint, and write a
        human-readable report to the SMB share. Values may contain commas/quotes/unicode,
        so this is plain text (not CSV) — no escaping surprises."""
        entries = []
        for u in unmatched:
            sug = nearest(u["value"], lookups)
            entries.append((u["value"], u["count"], sug[0] if sug else "", sug[2] if sug else ""))
        if entries:
            logger.warning("[smallsmt-stock] %d unmatched feeder value(s) — check for typos: %s",
                           len(entries), "; ".join(
                               f"{v!r}" + (f" ~ {nm!r}" if nm else "") for v, c, nm, ipn in entries))
        path_ = self.get_setting("UNMATCHED_REPORT_PATH")
        host = self.get_setting("SMB_HOST")
        if not (path_ and host):
            return
        lines = [
            f"Unmatched SmallSMT feeders: {len(entries)}",
            "These feeder values matched no InvenTree part (by name / MPN / IPN) and were skipped.",
            "'nearest' is the closest existing part — usually reveals a typo entered on the machine.",
            "",
        ]
        for v, c, nm, ipn in entries:
            lines.append(f"  - {v}    (count {c})")
            lines.append(f"        nearest:  {nm}   [{ipn}]" if nm else "        (no close match found)")
            lines.append("")
        report = "\n".join(lines)
        from .smb import write_bytes
        write_bytes(host, self.get_setting("SMB_SHARE"), path_, report.encode("utf-8"),
                    self.get_setting("SMB_USER"), self.get_setting("SMB_PASSWORD"), self.get_setting("SMB_DOMAIN"))

    def import_smt_stock(self):
        if not self.get_setting("IMPORT_ENABLED"):
            return
        s = self.run_import()
        print(f"[smt-stock] matched={s['matched']} created={s['created']} "
              f"updated={s['updated']} unchanged={s['unchanged']} unmatched={len(s['unmatched'])}")

    # HTTP trigger for testing: /plugin/smallsmt-stock/run
    def setup_urls(self):
        return [path("run", self.view_run, name="run")]

    def view_run(self, request):
        return JsonResponse(self.run_import())
