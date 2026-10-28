"""
ui_manifest.py -- optional manifest mods during setup or from the game library

Lists manifest .json files found in the Games folders, lets the user
tick mods, pick one option each, enter the shared CDN and a target
Games folder, then installs. Setup skips ahead when no manifests exist;
the library entry stays open with guidance.
"""

import html, os, threading

from qt import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QScrollArea, QCheckBox, QRadioButton,
    QButtonGroup, QProgressBar, QLineEdit, QPlainTextEdit, QMessageBox, QComboBox, QSizePolicy,
)
from qt import Qt

import config as cfg
import inhibit
import manifest_mods as mm
from net import _fmt_size

from ui_constants import (
    C_CARD, C_IW, C_TREY, C_DIM, C_DARK_BTN,
    font, _btn, _lbl, _badge, _header_bar, _log_to_file, _log_html, _copy_log_to_clipboard, _Sigs, go_to,
)

_NEXT_TO_JSON = "@json"
_BOX_CSS = f"color:#E5E5EC;background:#1A1A2A;border:1px solid {C_DIM};border-radius:8px;padding:10px 14px;"


class ManifestModsScreen(QWidget):
    def __init__(self, stack):
        super().__init__(); self.stack = stack; self.screen_name = "ManifestModsScreen"
        self._found = []; self._rows = []; self._busy = False; self._roots = []
        self._first_run = cfg.is_first_run(); self._scan_error = False

        lay = QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)
        lay.addWidget(_header_bar()[0])

        # Keep the choices and actions visible at handheld sizes; only the mod list scrolls.
        self._pick = QWidget(); clay = QVBoxLayout(self._pick)
        clay.setContentsMargins(24, 14, 24, 16); clay.setSpacing(10)
        clay.addWidget(_lbl("OPTIONAL MODS", 17, "#FFF", bold=True,
                            align=Qt.AlignmentFlag.AlignLeft, wrap=False))
        self.status = _lbl("Looking for mod manifests...", 11, C_DIM,
                           align=Qt.AlignmentFlag.AlignLeft)
        self.status.setAccessibleDescription("Mod scan status"); clay.addWidget(self.status)

        settings = QHBoxLayout(); settings.setSpacing(12)
        cdn_col = QVBoxLayout(); cdn_col.setSpacing(4)
        cdn_col.addWidget(_lbl("MOD CDN", 10, C_DIM, bold=True,
                               align=Qt.AlignmentFlag.AlignLeft, wrap=False))
        self.cdn = QLineEdit(); self.cdn.setPlaceholderText("cdn.example.com/mods")
        self.cdn.setAccessibleName("Mod CDN address"); self.cdn.setFixedHeight(38)
        self.cdn.setFont(font(11))
        self.cdn.setStyleSheet(f"QLineEdit{{background:{C_CARD};color:#FFF;border:1px solid #555568;"
                               f"border-radius:7px;padding:0 10px;}}QLineEdit:focus{{border-color:{C_IW};}}")
        cdn_col.addWidget(self.cdn); settings.addLayout(cdn_col, 1)

        root_col = QVBoxLayout(); root_col.setSpacing(4)
        root_col.addWidget(_lbl("INSTALL LOCATION", 10, C_DIM, bold=True,
                                align=Qt.AlignmentFlag.AlignLeft, wrap=False))
        self.root_choice = QComboBox(); self.root_choice.setFixedHeight(38)
        self.root_choice.setAccessibleName("Install location for selected mods")
        self.root_choice.setToolTip("Choose where the selected mods will be installed")
        self.root_choice.setStyleSheet(
            f"QComboBox{{background:{C_CARD};color:#FFF;border:1px solid #555568;"
            "border-radius:7px;padding:0 10px;}"
            f"QComboBox:focus{{border-color:{C_IW};}}"
            f"QComboBox QAbstractItemView{{background:{C_CARD};color:#FFF;selection-background-color:{C_DARK_BTN};}}")
        self.root_choice.currentIndexChanged.connect(lambda _i: self._refresh_rows())
        root_col.addWidget(self.root_choice); settings.addLayout(root_col, 1)
        clay.addLayout(settings)

        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setAccessibleName("Available mods and their install options")
        self._lw = QWidget(); self._list = QVBoxLayout(self._lw)
        self._list.setContentsMargins(0, 0, 4, 0); self._list.setSpacing(8); self._list.addStretch()
        scroll.setWidget(self._lw); clay.addWidget(scroll, stretch=1)

        self.warning = _lbl("", 11, C_TREY, align=Qt.AlignmentFlag.AlignLeft)
        self.warning.setAccessibleDescription("Mod selection warning")
        self.warning.setVisible(False); clay.addWidget(self.warning)

        brow = QHBoxLayout(); brow.setSpacing(8)
        self.back = _btn("Back", C_DARK_BTN, size=11, h=44)
        self.back.setAccessibleName("Back to setup")
        self.back.clicked.connect(self._go_back)
        self.skip = _btn("Skip mods", C_DARK_BTN, size=11, h=44)
        self.skip.setAccessibleName("Skip mods and scan games")
        self.skip.clicked.connect(self._go_next)
        self.inst = _btn("Install selected", C_IW, size=11, h=44)
        self.inst.clicked.connect(self._install)
        brow.addWidget(self.back, 1); brow.addWidget(self.skip, 1)
        brow.addWidget(self.inst, 2); clay.addLayout(brow)
        lay.addWidget(self._pick, stretch=1)

        # Progress, log and results share the space without forcing the actions off screen.
        self._run = QWidget(); rlay = QVBoxLayout(self._run)
        rlay.setContentsMargins(24, 16, 24, 16); rlay.setSpacing(10)
        self.cur = _lbl("Preparing...", 14, "#E5E5EC", bold=True,
                        align=Qt.AlignmentFlag.AlignLeft)
        self.cur.setAccessibleDescription("Mod operation status"); rlay.addWidget(self.cur)
        self.bar = QProgressBar(); self.bar.setRange(0, 100)
        self.bar.setFormat("%p% complete"); self.bar.setFixedHeight(22)
        self.bar.setAccessibleName("Mod operation progress")
        rlay.addWidget(self.bar)

        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setFont(font(10))
        self.log.setAccessibleName("Mod operation log")
        self.log.setMinimumHeight(80)
        self.log.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.log.setStyleSheet(f"QPlainTextEdit{{color:#BDBDCD;background:{C_CARD};"
                               "border:1px solid #444455;border-radius:8px;padding:8px;}")
        rlay.addWidget(self.log, stretch=1)
        self._log_status = _lbl("", 10, C_DIM, align=Qt.AlignmentFlag.AlignLeft)
        log_btn = _btn("Copy log", C_DARK_BTN, size=10, h=36)
        log_btn.clicked.connect(lambda: (self._log_status.setVisible(True), _copy_log_to_clipboard(self._log_status)))
        lr = QHBoxLayout(); lr.setSpacing(10)
        lr.addWidget(log_btn); lr.addWidget(self._log_status, 1)
        rlay.addLayout(lr)

        self.summary = _lbl("", 11, "#E5E5EC", align=Qt.AlignmentFlag.AlignLeft)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.summary.setStyleSheet(_BOX_CSS)
        self.summary.setAccessibleDescription("Mod operation results")
        self._summary_scroll = QScrollArea(); self._summary_scroll.setWidgetResizable(True)
        self._summary_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._summary_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._summary_scroll.setFixedHeight(104)
        self._summary_scroll.setWidget(self.summary); self._summary_scroll.setVisible(False)
        rlay.addWidget(self._summary_scroll)

        self.mods_btn = _btn("Back to mods", C_DARK_BTN, size=11, h=44)
        self.mods_btn.clicked.connect(self._show_pick)
        self.cont = _btn("Continue to game scan", C_IW, size=11, h=44)
        self.cont.clicked.connect(self._go_next)
        cw = QHBoxLayout(); cw.setSpacing(8)
        cw.addWidget(self.mods_btn, 1); cw.addWidget(self.cont, 1)
        rlay.addLayout(cw)
        self._run.setVisible(False); lay.addWidget(self._run, stretch=1)

    def showEvent(self, e):
        super().showEvent(e)
        # Window restored (not a screen change): keep the user's picks
        if self._busy or e.spontaneous(): return
        self._first_run = cfg.is_first_run()
        self.back.setText("Back" if self._first_run else "Back to library")
        self.back.setAccessibleName("Back to setup" if self._first_run else "Back to game library")
        self.skip.setText("Skip mods" if self._first_run else "Close mods")
        self.skip.setAccessibleName("Skip mods and scan games" if self._first_run else "Close mods and return to game library")
        self.cont.setText("Continue to game scan" if self._first_run else "Return to library")
        self.cont.setAccessibleName("Continue to game scan" if self._first_run else "Return to game library")
        self._show_pick(); self._set_enabled(False)
        self.status.setText("Looking for mod manifests...")
        self._sigs = _Sigs(); self._sigs.done.connect(self._on_scanned)
        threading.Thread(target=self._scan, daemon=True).start()

    def _go_back(self):
        go_to(self.stack, "SetupFlowScreen" if self._first_run else "ManagementScreen")

    def _go_next(self):
        go_to(self.stack, "WelcomeScreen" if self._first_run else "ManagementScreen")

    def _show_pick(self):
        self._run.setVisible(False); self._pick.setVisible(True); self.warning.setVisible(False)

    def _scan(self):
        self._scan_error = False
        try:
            self._found = mm.scan_manifests(); self._roots = mm.games_roots()
        except Exception as ex:
            _log_to_file(f"[ManifestModsScreen] scan failed: {ex}")
            self._found = []; self._roots = []; self._scan_error = True
        self._sigs.done.emit(bool(self._found))

    def _on_scanned(self, any_found):
        # The setup wizard skips this optional step; the library's Mods entry stays open.
        if not any_found and self._first_run:
            self._go_next(); return
        if not self.cdn.text(): self.cdn.setText(mm.get_saved_cdn())
        self._build_roots(); self._build_rows(); self._set_enabled(True)
        if not any_found:
            self.status.setText("Could not scan for mod manifests." if self._scan_error else "No mod manifests found.")
            detail = ("Check that your Games folders are accessible, then close and reopen Mods to scan again."
                      if self._scan_error else
                      "Place a supported manifest .json file in a Games folder (internal storage or SD card), "
                      "then close and reopen Mods to scan again. Your installed games are unchanged.")
            empty = _lbl(detail, 12, "#E5E5EC", align=Qt.AlignmentFlag.AlignLeft)
            empty.setStyleSheet(_BOX_CSS)
            self._list.insertWidget(0, empty)
            self.cdn.setEnabled(False); self.root_choice.setEnabled(False)
            return
        ok = [f for f in self._found if "manifest" in f]; bad = len(self._found) - len(ok)
        action = "skip to the game scan" if self._first_run else "return to your game library"
        if not ok:
            self.status.setText(f"No usable mods found. Review the {bad} unreadable manifest(s) below, or {action}.")
            self.cdn.setEnabled(False); self.root_choice.setEnabled(False)
        else:
            self.status.setText(f"{len(ok)} mod(s) found. Choose mods and options below, or {action}."
                                + (f" {bad} manifest(s) could not be read." if bad else ""))

    def _build_roots(self):
        previous = self._root()
        home = os.path.expanduser("~")
        self.root_choice.blockSignals(True)
        self.root_choice.clear()
        # Default: each mod goes to the Games folder its manifest is in; a drive sends all there.
        for r in [_NEXT_TO_JSON] + self._roots:
            name = ("Next to each manifest" if r == _NEXT_TO_JSON else
                    "Internal: ~/" + os.path.relpath(r, home) if r.startswith(home + os.sep)
                    else f"SD / drive: {r}")
            self.root_choice.addItem(name, r)
            self.root_choice.setItemData(self.root_choice.count() - 1, r, Qt.ItemDataRole.ToolTipRole)
        index = self.root_choice.findData(previous)
        self.root_choice.setCurrentIndex(index if index >= 0 else 0)
        self.root_choice.blockSignals(False)

    def _root(self):
        return self.root_choice.currentData() or ""

    def _target(self, r):
        root = self._root()
        return r["dir"] if root == _NEXT_TO_JSON else root

    @staticmethod
    def _choice_line(button, title, detail=""):
        """A full-width choice with a wrapping label, instead of clipped option pills."""
        w = QWidget(); w.setObjectName("mmoption")
        w.setStyleSheet("QWidget#mmoption{background:#292932;border-radius:6px;}")
        row = QHBoxLayout(w); row.setContentsMargins(10, 6, 10, 6); row.setSpacing(8)
        button.setAccessibleName(title)
        button.setMinimumHeight(30); row.addWidget(button, alignment=Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout(); text.setSpacing(2)
        label = _lbl(title, 11, "#F0F0F4", align=Qt.AlignmentFlag.AlignLeft)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setCursor(Qt.CursorShape.PointingHandCursor)
        label.mousePressEvent = lambda _e: button.isEnabled() and button.click()
        text.addWidget(label)
        if detail:
            note = _lbl(detail, 10, "#B6B6C5", align=Qt.AlignmentFlag.AlignLeft)
            note.setTextFormat(Qt.TextFormat.PlainText)
            text.addWidget(note)
        row.addLayout(text, 1)
        return w

    def _build_rows(self):
        while self._list.count() > 1:
            it = self._list.takeAt(0)
            if it.widget(): it.widget().deleteLater()
        self._rows = []
        for f in self._found:
            card = QFrame(); card.setObjectName("mmcard")
            card.setStyleSheet(f"QFrame#mmcard{{background:{C_CARD};border-top:3px solid #444455;border-radius:8px;}}")
            v = QVBoxLayout(card); v.setContentsMargins(12, 10, 12, 10); v.setSpacing(7)
            if "error" in f:
                error = _lbl(f"Could not read {f['file']}: {f['error']}", 11, C_TREY,
                             align=Qt.AlignmentFlag.AlignLeft)
                error.setTextFormat(Qt.TextFormat.PlainText); v.addWidget(error)
                self._list.insertWidget(self._list.count() - 1, card); continue
            m = f["manifest"]
            head = QHBoxLayout(); head.setSpacing(8)
            cb = QCheckBox(); cb.setAccessibleName(f"Select {m['name']} for installation")
            cb.setMinimumHeight(32); head.addWidget(cb, alignment=Qt.AlignmentFlag.AlignTop)
            title = m["name"] + (f"  ·  v{m['version']}" if m["version"] else "")
            name = _lbl(title, 12, "#FFF", bold=True, align=Qt.AlignmentFlag.AlignLeft)
            name.setTextFormat(Qt.TextFormat.PlainText)
            name.setCursor(Qt.CursorShape.PointingHandCursor)
            name.mousePressEvent = lambda _e, b=cb: b.isEnabled() and b.setChecked(not b.isChecked())
            head.addWidget(name, 1)
            state = _badge("", C_IW, 108, h=24, size=9, radius=4)
            state.setVisible(False); head.addWidget(state, alignment=Qt.AlignmentFlag.AlignTop)
            unin = _btn("Uninstall", C_DARK_BTN, size=10, h=32)
            unin.setAccessibleName(f"Uninstall {m['name']} from selected location")
            unin.setVisible(False); head.addWidget(unin, alignment=Qt.AlignmentFlag.AlignTop)
            v.addLayout(head)
            if m["description"]:
                desc = _lbl(m["description"], 10, "#B6B6C5", align=Qt.AlignmentFlag.AlignLeft)
                desc.setTextFormat(Qt.TextFormat.PlainText); v.addWidget(desc)
            choices = QVBoxLayout(); choices.setContentsMargins(30, 0, 0, 0); choices.setSpacing(5)
            if m["mode"] == "components":
                grp, boxes = None, []
                for c in m["components"]:
                    if not c["show"]: continue
                    ob = QCheckBox(); ob.setProperty("oid", c["id"])
                    ob.setProperty("req", c["required"]); ob.setProperty("def", c["default"])
                    ob.setChecked(c["required"] or c["default"]); ob.setEnabled(not c["required"])
                    title = f"{c['name']}  ·  {_fmt_size(sum(x['size'] for x in c['files']))}"
                    if c["required"]: title += "  ·  Required"
                    choices.addWidget(self._choice_line(ob, title, c["description"]))
                    # Ticking a part ticks the mod without re-running the mod's check-all.
                    ob.toggled.connect(lambda on, mc=cb: (on and not mc.isChecked() and (mc.blockSignals(True), mc.setChecked(True), mc.blockSignals(False)),
                                                          self._update_total()))
                    boxes.append(ob)
                cb.toggled.connect(lambda on, bs=boxes: ([b.setChecked(on) for b in bs if not b.property("req")], self._update_total()))
            else:
                grp, boxes = QButtonGroup(card), None
                for i, o in enumerate(m["options"]):
                    b = QRadioButton(); b.setProperty("oid", o["id"])
                    grp.addButton(b); b.setChecked(i == 0)
                    title = f"{o['name']}  ·  {_fmt_size(mm.total_size(o))}"
                    choices.addWidget(self._choice_line(b, title, o["description"]))
                cb.toggled.connect(lambda _on: self._update_total())
                grp.buttonClicked.connect(lambda _b, mc=cb: (mc.setChecked(True), self._update_total()))
            v.addLayout(choices)
            row = {"m": m, "dir": os.path.dirname(f["path"]), "cb": cb, "grp": grp, "boxes": boxes,
                   "state": state, "card": card, "unin": unin}
            unin.clicked.connect(lambda _c=False, rr=row: self._uninstall(rr))
            self._rows.append(row)
            self._list.insertWidget(self._list.count() - 1, card)
        self._refresh_rows()

    def _set_state(self, r, text, color):
        r["card"].setStyleSheet(f"QFrame#mmcard{{background:{C_CARD};border-top:3px solid {color or '#444455'};border-radius:8px;}}")
        b = r["state"]; b.setVisible(bool(text)); b.setText(text)
        b.setAccessibleName(f"{r['m']['name']}: {text.lower()}" if text else "")
        b.setStyleSheet(f"QPushButton{{background:{color};color:#151518;border:none;border-radius:4px;}}"
                        f"QPushButton:disabled{{background:{color};color:#151518;}}")

    def _refresh_rows(self):
        for r in self._rows:
            root = self._target(r); mid = r["m"]["id"]; iroot = mm.install_root(root, r["m"]) if root else ""
            rec = mm.get_receipt(iroot, mid) if iroot else {}
            r["unin"].setVisible(bool(rec))
            if r["boxes"] is not None:
                # Tick what is installed in this Games folder, so installing more parts keeps the old ones.
                have = set((rec.get("option") or "").split("+")) if rec else None
                for b in r["boxes"]:
                    if b.property("req"): continue
                    b.blockSignals(True); b.setChecked(b.property("oid") in have if have is not None else bool(b.property("def"))); b.blockSignals(False)
            elif rec.get("option"):
                for b in r["grp"].buttons():
                    if b.property("oid") == rec["option"]: b.setChecked(True)
            if not rec:
                self._set_state(r, "", "")
            elif not rec.get("complete"):
                self._set_state(r, "INCOMPLETE", C_TREY); r["state"].setToolTip("Install again to finish")
            elif mm.needs_update(r["m"], iroot):
                self._set_state(r, "UPDATE", C_TREY); r["state"].setToolTip("")
            else:
                try: name = mm.get_option(r["m"], rec.get("option"))["name"]
                except mm.ManifestError: name = ""
                self._set_state(r, "INSTALLED", C_IW); r["state"].setToolTip(name)
        self._update_total()

    def _selected(self):
        out = []
        for r in self._rows:
            if not r["cb"].isChecked(): continue
            if r["boxes"] is not None:
                out.append((r["m"], "+".join(b.property("oid") for b in r["boxes"] if b.isChecked()), self._target(r)))
            elif r["grp"].checkedButton():
                out.append((r["m"], r["grp"].checkedButton().property("oid"), self._target(r)))
        return out

    def _update_total(self):
        sel = self._selected()
        size = sum(mm.total_size(mm.get_option(m, oid)) for m, oid, _r in sel)
        count = len(sel)
        self.inst.setText(f"Install {count} mod{'s' if count != 1 else ''} · {_fmt_size(size)}" if sel else "Select mods to install")
        self.inst.setAccessibleName(f"Install {count} selected mod{'s' if count != 1 else ''}" if sel else "Select mods to install")
        self.inst.setEnabled(bool(sel) and not self._busy)

    def _set_enabled(self, on):
        self.back.setEnabled(on); self.skip.setEnabled(on); self.cdn.setEnabled(on); self._lw.setEnabled(on)
        self.root_choice.setEnabled(on)
        if on: self._update_total()
        else: self.inst.setEnabled(False)

    def _warn(self, text):
        self.warning.setText(text); self.warning.setVisible(True)

    def _install(self):
        sel = self._selected()
        if not sel: return
        if not self._root(): self._warn("Choose where to install the mods."); return
        try:
            cdn = mm.normalize_cdn(self.cdn.text())
        except mm.ManifestError as ex:
            self._warn(f"Mod CDN: {ex}"); self.cdn.setFocus(); return
        self._busy = True; self._set_enabled(False)
        self._pick.setVisible(False); self._run.setVisible(True)
        self.cur.setText(f"Installing {len(sel)} mod{'s' if len(sel) != 1 else ''}...")
        self.cur.setStyleSheet("color:#E5E5EC;background:transparent;")
        self.bar.setRange(0, 100); self.bar.setValue(0); self.bar.setTextVisible(True)
        self.log.clear(); self._log_status.setText(""); self._log_status.setVisible(False)
        self._summary_scroll.setVisible(False); self.mods_btn.setVisible(False); self.cont.setVisible(False)
        self._isigs = _Sigs()
        self._isigs.progress.connect(lambda p, m: (self.bar.setValue(p), self.cur.setText(m)))
        self._isigs.log.connect(self._append_log)
        self._isigs.done.connect(self._on_installed)
        threading.Thread(target=self._do_install, args=(sel, cdn), daemon=True).start()

    def _do_install(self, sel, cdn):
        self._results = []; n = len(sel)
        log = self._isigs.log.emit
        inhibit.start("DeckOps is installing mods")
        try:
            for i, (m, oid, root) in enumerate(sel):
                opt = mm.get_option(m, oid)
                log(f"--- {m['name']} ({opt['name']}) ---")
                tag = f"[{i + 1}/{n}] {m['name']}: "
                cb = lambda p, msg, _i=i, _t=tag: self._isigs.progress.emit(int((_i * 100 + p) / n), _t + msg)
                try:
                    errs = mm.install_manifest(m, oid, cdn, root, cb, on_log=log)
                    if errs:
                        log(f"✗ {len(errs)} file(s) failed")
                    else:
                        log(f"✓ {opt['name']} installed")
                    self._results.append((m, oid, errs, ""))
                except mm.ManifestError as ex:
                    log(f"✗ {ex}")
                    self._results.append((m, oid, None, str(ex)))
                except Exception as ex:
                    _log_to_file(f"[ManifestModsScreen] {m['id']} install failed: {ex}")
                    log(f"✗ unexpected error: {ex}")
                    self._results.append((m, oid, None, "unexpected error, see log"))
        finally:
            inhibit.stop()
        self._isigs.done.emit(all(e == [] for _m, _o, e, _x in self._results))

    def _uninstall(self, r):
        """Remove an installed mod from the chosen Games folder."""
        if self._busy:
            return
        root = self._target(r)
        if not root:
            self._warn("Choose the Games folder the mod is installed in."); return
        iroot = mm.install_root(root, r["m"])
        if QMessageBox.question(
                self, "Uninstall Mod",
                f"Remove {r['m']['name']} from {iroot}?\n\n"
                "Files it replaced are put back. Files you changed since are kept.") \
                != QMessageBox.StandardButton.Yes:
            return
        self._busy = True; self._set_enabled(False)
        self._pick.setVisible(False); self._run.setVisible(True)
        self.cur.setText(f"Removing {r['m']['name']}...")
        self.cur.setStyleSheet("color:#E5E5EC;background:transparent;")
        self.bar.setRange(0, 0); self.bar.setTextVisible(False)  # No uninstall percentages.
        self.log.clear(); self._log_status.setText(""); self._log_status.setVisible(False)
        self._summary_scroll.setVisible(False); self.mods_btn.setVisible(False); self.cont.setVisible(False)
        self._usigs = _Sigs()
        self._usigs.log.connect(self._append_log)
        self._usigs.done.connect(self._on_uninstalled)
        threading.Thread(target=self._do_uninstall, args=(r["m"], iroot, root), daemon=True).start()

    def _do_uninstall(self, m, iroot, root):
        log = self._usigs.log.emit
        log(f"--- {m['name']}: uninstall from {iroot} ---")
        inhibit.start("DeckOps is removing a mod")
        try:
            left = mm.uninstall_manifest(m["id"], iroot, on_log=log, games_root=root)
            self._uresult = (m, left, "")
        except mm.ManifestError as ex:
            log(f"✗ {ex}")
            self._uresult = (m, None, str(ex))
        except Exception as ex:
            _log_to_file(f"[ManifestModsScreen] {m['id']} uninstall failed: {ex}")
            log(f"✗ unexpected error: {ex}")
            self._uresult = (m, None, "unexpected error, see log")
        finally:
            inhibit.stop()
        self._usigs.done.emit(self._uresult[1] is not None)

    def _on_uninstalled(self, ok):
        self._busy = False; self.bar.setRange(0, 100); self.bar.setValue(100); self.bar.setTextVisible(True)
        m, left, msg = self._uresult
        name = html.escape(m["name"])
        if ok:
            text = f'<span style="color:{C_IW}">&#10003; {name} removed</span>'
            if left:
                text += f'<br><span style="color:#E5E5EC">{len(left)} changed file(s) kept. See the log for details.</span>'
        else:
            text = f'<span style="color:{C_TREY}">&#10007; {name}: {html.escape(msg)}</span>'
            text += '<br>Review the log, then return to mods to try again.'
        self.summary.setText(text); self._summary_scroll.setVisible(True)
        self.cur.setText("Mod removed" if ok else "Could not remove the mod")
        self.cur.setStyleSheet(f"color:{C_IW if ok else C_TREY};background:transparent;")
        self.mods_btn.setVisible(True); self.cont.setVisible(True)
        self._set_enabled(True); self._refresh_rows()

    def _append_log(self, text):
        _log_to_file(text)
        sb = self.log.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        self.log.appendHtml(_log_html(text))
        if at_bottom: sb.setValue(sb.maximum())

    def _on_installed(self, all_ok):
        self._busy = False; self.bar.setValue(100)
        lines = []
        for m, oid, errs, msg in self._results:
            name = html.escape(f"{m['name']} ({mm.get_option(m, oid)['name']})")
            if errs == []:
                lines.append(f'<span style="color:{C_IW}">&#10003; {name} installed</span>')
            elif errs:
                lines.append(f'<span style="color:{C_TREY}">&#10007; {name}: {len(errs)} file(s) failed. '
                             f'Install again to retry only those.</span>')
            else:
                lines.append(f'<span style="color:{C_TREY}">&#10007; {name}: {html.escape(msg)}</span>')
        if not all_ok:
            lines.append('<br>Review the log, then return to mods to retry failed files.')
        self.summary.setText("<br>".join(lines)); self._summary_scroll.setVisible(True)
        self.cur.setText("Mods installed" if all_ok else "Finished with errors")
        self.cur.setStyleSheet(f"color:{C_IW if all_ok else C_TREY};background:transparent;")
        self.mods_btn.setVisible(True); self.cont.setVisible(True)
        self._set_enabled(True); self._refresh_rows()
