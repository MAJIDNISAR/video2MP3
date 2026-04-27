import glob
import os
import queue
import re
import tempfile
import threading
import time
import tkinter as tk
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox, ttk

try:
    from moviepy import VideoFileClip
except ImportError:
    from moviepy.editor import VideoFileClip

from proglog import ProgressBarLogger


CPU_COUNT = max(1, os.cpu_count() or 1)
MAX_RETRIES = 3
MAX_FILENAME_LEN = 200


def _sanitize_filename(name: str) -> str:
    """Normalize unicode, strip problematic chars, and truncate long names."""
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.encode("utf-8", errors="replace").decode("utf-8")
    base, ext = os.path.splitext(name)
    max_base = MAX_FILENAME_LEN - len(ext.encode("utf-8"))
    while len(base.encode("utf-8")) > max_base:
        base = base[:-1]
    return base + ext


def _display_name(path: str, max_chars: int = 60) -> str:
    """Return a truncated basename for UI display."""
    name = os.path.basename(path)
    if len(name) <= max_chars:
        return name
    half = (max_chars - 3) // 2
    return name[:half] + "..." + name[-half:]


_UNRECOVERABLE_PATTERNS = (
    "moov atom not found",
    "Invalid data found when processing input",
    "not found",
    "No such file or directory",
    "Permission denied",
)


def _is_unrecoverable(error: str) -> bool:
    """Return True if the error will never succeed on retry."""
    return any(pat in error for pat in _UNRECOVERABLE_PATTERNS)


def _needs_safe_path(path: str) -> bool:
    """Return True if the path has characters that confuse ffmpeg's shell parsing."""
    basename = os.path.basename(path)
    if "'" in basename or '"' in basename:
        return True
    try:
        basename.encode("ascii")
    except UnicodeEncodeError:
        return True
    return False


def _resolve_path(src: str) -> str:
    """Try NFC and NFD normalization to find the actual file on disk."""
    if os.path.exists(src):
        return src
    nfc = unicodedata.normalize("NFC", src)
    if os.path.exists(nfc):
        return nfc
    nfd = unicodedata.normalize("NFD", src)
    if os.path.exists(nfd):
        return nfd
    # Last resort: scan the directory for a matching basename
    parent = os.path.dirname(src)
    target = os.path.basename(src)
    if os.path.isdir(parent):
        for entry in os.listdir(parent):
            if unicodedata.normalize("NFC", entry) == unicodedata.normalize("NFC", target):
                return os.path.join(parent, entry)
    return src  # return original, let ffmpeg report the real error


def _make_safe_symlink(src: str) -> str:
    """Create a temp symlink with an ASCII-safe name pointing to src."""
    resolved = _resolve_path(src)
    _, ext = os.path.splitext(resolved)
    fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="mp4conv_")
    os.close(fd)
    os.remove(tmp_path)
    os.symlink(os.path.abspath(resolved), tmp_path)
    return tmp_path


class _SkipRequested(Exception):
    """Raised from the proglog callback when the user asks to skip / cancel."""


class _TkProgressLogger(ProgressBarLogger):
    """Forwards moviepy bar progress (0-100) per-path; aborts on skip/cancel."""

    def __init__(
        self,
        path: str,
        msg_queue: queue.Queue,
        skip_event: threading.Event,
        cancel_event: threading.Event,
    ) -> None:
        super().__init__()
        self._path = path
        self._q = msg_queue
        self._skip = skip_event
        self._cancel = cancel_event
        self._last_int_pct = -1

    def bars_callback(self, bar, attr, value, old_value=None) -> None:
        if self._cancel.is_set() or self._skip.is_set():
            raise _SkipRequested()
        if attr != "index":
            return
        total = self.bars[bar].get("total")
        if not total:
            return
        pct = min(100.0, (value / total) * 100.0)
        int_pct = int(pct)
        if int_pct != self._last_int_pct:
            self._last_int_pct = int_pct
            self._q.put(("file_progress", {"path": self._path, "pct": pct}))


def _convert_one(
    src: str,
    msg_queue: queue.Queue,
    cancel_event: threading.Event,
    skip_event: threading.Event,
) -> str:
    raw_base, _ = os.path.splitext(os.path.basename(src))
    safe_name = _sanitize_filename(raw_base + ".mp3")
    output_path = os.path.join(os.path.dirname(src), safe_name)
    if os.path.exists(output_path):
        return "skipped"
    if cancel_event.is_set():
        return "cancelled"

    # Use a symlink with an ASCII-safe name when the original path would
    # confuse ffmpeg's command-line parsing (quotes, non-ASCII chars).
    safe_link: str | None = None
    input_path = src
    if _needs_safe_path(src):
        safe_link = _make_safe_symlink(src)
        input_path = safe_link

    def _cleanup_partial() -> None:
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass

    def _cleanup_link() -> None:
        if safe_link and os.path.islink(safe_link):
            try:
                os.remove(safe_link)
            except OSError:
                pass

    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        if cancel_event.is_set():
            _cleanup_link()
            return "cancelled"
        if skip_event.is_set():
            _cleanup_link()
            return "skipped_user"

        logger = _TkProgressLogger(src, msg_queue, skip_event, cancel_event)
        try:
            with VideoFileClip(input_path) as video:
                if video.audio is None:
                    _cleanup_link()
                    return "no_audio"
                video.audio.write_audiofile(output_path, logger=logger)
            _cleanup_link()
            return "ok"
        except _SkipRequested:
            _cleanup_partial()
            _cleanup_link()
            return "cancelled" if cancel_event.is_set() else "skipped_user"
        except Exception as exc:
            last_exc = exc
            _cleanup_partial()
            err_str = str(exc)

            # Don't retry unrecoverable errors (corrupt files, missing files, etc.)
            if _is_unrecoverable(err_str):
                msg_queue.put((
                    "error_detail",
                    {"path": src, "error": err_str},
                ))
                _cleanup_link()
                return "failed"

            if attempt < MAX_RETRIES:
                msg_queue.put((
                    "retry",
                    {"path": src, "attempt": attempt, "error": err_str},
                ))
            else:
                msg_queue.put((
                    "error_detail",
                    {"path": src, "error": str(last_exc)},
                ))

    _cleanup_link()
    return "failed"


def _worker(
    files: list[str],
    workers: int,
    msg_queue: queue.Queue,
    cancel_event: threading.Event,
    skip_events: dict[str, threading.Event],
) -> None:
    total = len(files)
    completed = 0

    def task(src: str) -> tuple[str, str]:
        msg_queue.put(("file_start", {"path": src}))
        skip_event = skip_events[src]
        status = _convert_one(src, msg_queue, cancel_event, skip_event)
        return src, status

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(task, src) for src in files]
        for fut in futures:
            try:
                src, status = fut.result()
            except Exception as exc:
                msg_queue.put(("error_detail", {"path": "unknown", "error": f"worker error: {exc}"}))
                continue
            msg_queue.put(("file_done", {"path": src, "status": status}))
            completed += 1
    msg_queue.put((
        "done",
        {"cancelled": cancel_event.is_set(), "completed": completed, "total": total},
    ))


STATUS_LABELS = {
    "pending": "Pending",
    "ok": "Done",
    "skipped": "Skipped (exists)",
    "skipped_user": "Skipped (manual)",
    "no_audio": "No audio track",
    "cancelled": "Cancelled",
    "failed": "Failed",
}


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.queue_paths: list[str] = []
        self.msg_q: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.skip_events: dict[str, threading.Event] = {}
        self.worker_thread: threading.Thread | None = None
        self.progress_by_path: dict[str, float] = {}
        self.completed_count: int = 0

        self._build_ui()
        self._schedule_poll()

    def _build_ui(self) -> None:
        self.root.title("MP4 → MP3 Converter")
        self.root.geometry("1020x720")
        self.root.minsize(860, 580)

        style = ttk.Style()
        for theme in ("aqua", "clam", "default"):
            if theme in style.theme_names():
                try:
                    style.theme_use(theme)
                    break
                except tk.TclError:
                    continue

        style.configure("Header.TLabel", font=("Helvetica", 22, "bold"))
        style.configure("Sub.TLabel", font=("Helvetica", 11), foreground="#6b6b6b")
        style.configure("Status.TLabel", font=("Helvetica", 11))
        style.configure("Treeview", rowheight=26)
        style.configure("Treeview.Heading", font=("Helvetica", 11, "bold"))

        header = ttk.Frame(self.root, padding=(24, 18, 24, 6))
        header.pack(fill=tk.X)
        ttk.Label(header, text="MP4 → MP3 Converter", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text=f"Parallel conversion across CPU cores. MP3s are written next to each source.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        toolbar = ttk.Frame(self.root, padding=(24, 4))
        toolbar.pack(fill=tk.X)
        ttk.Button(toolbar, text="Add Files", command=self._add_files).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Add Folder", command=self._add_folder).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="Remove Selected", command=self._remove_selected).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="Clear", command=self._clear).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(toolbar, text="Threads:").pack(side=tk.LEFT, padx=(20, 4))
        self.threads_var = tk.IntVar(value=CPU_COUNT)
        self.threads_spin = ttk.Spinbox(
            toolbar,
            from_=1,
            to=max(2, CPU_COUNT * 2),
            textvariable=self.threads_var,
            width=4,
        )
        self.threads_spin.pack(side=tk.LEFT)
        ttk.Label(
            toolbar,
            text=f"(detected {CPU_COUNT} cores)",
            style="Sub.TLabel",
        ).pack(side=tk.LEFT, padx=(6, 0))

        list_frame = ttk.Frame(self.root, padding=(24, 10))
        list_frame.pack(fill=tk.BOTH, expand=True)

        self.tree = ttk.Treeview(
            list_frame,
            columns=("status", "progress", "file", "path", "error"),
            show="headings",
            selectmode="extended",
        )
        self.tree.heading("status", text="Status")
        self.tree.heading("progress", text="Progress")
        self.tree.heading("file", text="File")
        self.tree.heading("path", text="Folder")
        self.tree.heading("error", text="Error")
        self.tree.column("status", width=130, anchor="w", stretch=False)
        self.tree.column("progress", width=70, anchor="center", stretch=False)
        self.tree.column("file", width=220, anchor="w")
        self.tree.column("path", width=240, anchor="w")
        self.tree.column("error", width=200, anchor="w")

        self.tree.tag_configure("ok", foreground="#117a3d")
        self.tree.tag_configure("failed", foreground="#b00020")
        self.tree.tag_configure("skipped", foreground="#8a6d00")
        self.tree.tag_configure("converting", foreground="#0a58ca")

        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        progress_frame = ttk.Frame(self.root, padding=(24, 6))
        progress_frame.pack(fill=tk.X)
        self.overall_label = ttk.Label(progress_frame, text="Idle", style="Status.TLabel")
        self.overall_label.pack(anchor="w")
        self.overall_bar = ttk.Progressbar(progress_frame, mode="determinate", maximum=100)
        self.overall_bar.pack(fill=tk.X, pady=(2, 6))

        # --- Collapsible log panel ---
        self._log_visible = tk.BooleanVar(value=False)
        log_toggle_frame = ttk.Frame(self.root, padding=(24, 0))
        log_toggle_frame.pack(fill=tk.X)
        self.log_toggle_btn = ttk.Checkbutton(
            log_toggle_frame,
            text="Show Log",
            variable=self._log_visible,
            command=self._toggle_log,
            style="Toolbutton",
        )
        self.log_toggle_btn.pack(side=tk.LEFT)
        self.log_clear_btn = ttk.Button(
            log_toggle_frame, text="Clear Log", command=self._clear_log
        )
        self.log_clear_btn.pack(side=tk.LEFT, padx=(8, 0))

        self.log_frame = ttk.Frame(self.root, padding=(24, 4))
        # starts hidden — not packed yet

        self.log_text = tk.Text(
            self.log_frame,
            height=10,
            wrap=tk.WORD,
            font=("Menlo", 10),
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="#d4d4d4",
            relief=tk.SUNKEN,
            borderwidth=1,
        )
        # Allow selection & copy but block editing
        self.log_text.bind("<Key>", self._log_key_filter)
        self.log_text.tag_configure("timestamp", foreground="#6a9955")
        self.log_text.tag_configure("info", foreground="#d4d4d4")
        self.log_text.tag_configure("success", foreground="#4ec9b0")
        self.log_text.tag_configure("warn", foreground="#dcdcaa")
        self.log_text.tag_configure("error", foreground="#f44747")
        self.log_text.tag_configure("progress", foreground="#569cd6")

        # Right-click context menu
        self._log_menu = tk.Menu(self.log_text, tearoff=0)
        self._log_menu.add_command(label="Copy Selection", command=self._copy_log_selection)
        self._log_menu.add_command(label="Copy All", command=self._copy_log_all)
        self._log_menu.add_separator()
        self._log_menu.add_command(label="Select All", command=self._select_log_all)
        self.log_text.bind("<Button-2>", self._show_log_menu)
        self.log_text.bind("<Control-Button-1>", self._show_log_menu)
        if self.root.tk.call("tk", "windowingsystem") == "aqua":
            self.log_text.bind("<Button-3>", self._show_log_menu)

        log_scroll = ttk.Scrollbar(
            self.log_frame, orient="vertical", command=self.log_text.yview
        )
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        action = ttk.Frame(self.root, padding=(24, 12, 24, 18))
        action.pack(fill=tk.X)
        self.convert_btn = ttk.Button(action, text="Convert", command=self._start_convert)
        self.convert_btn.pack(side=tk.LEFT)
        self.skip_btn = ttk.Button(
            action, text="Skip Selected", command=self._skip_selected, state=tk.DISABLED
        )
        self.skip_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.cancel_btn = ttk.Button(
            action, text="Cancel All", command=self._cancel, state=tk.DISABLED
        )
        self.cancel_btn.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(action, text="Quit", command=self._quit).pack(side=tk.RIGHT)

        self.root.protocol("WM_DELETE_WINDOW", self._quit)

    def _add_files(self) -> None:
        selected = filedialog.askopenfilenames(
            title="Choose MP4 file(s)",
            filetypes=(("MP4 files", "*.mp4 *.MP4"), ("All files", "*.*")),
        )
        if selected:
            self._add_to_queue(selected)

    def _add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Choose folder of MP4s")
        if not folder:
            return
        matches = sorted(
            glob.glob(os.path.join(folder, "*.mp4"))
            + glob.glob(os.path.join(folder, "*.MP4"))
        )
        if not matches:
            messagebox.showinfo("No MP4 files", f"No .mp4 files found in:\n{folder}")
            return
        self._add_to_queue(matches)

    def _add_to_queue(self, paths) -> None:
        for path in paths:
            if path in self.queue_paths:
                continue
            self.queue_paths.append(path)
            self.tree.insert(
                "",
                tk.END,
                iid=path,
                values=(
                    STATUS_LABELS["pending"],
                    "",
                    _display_name(path),
                    os.path.dirname(path),
                    "",
                ),
            )

    def _remove_selected(self) -> None:
        if self._is_running():
            return
        for iid in self.tree.selection():
            if iid in self.queue_paths:
                self.queue_paths.remove(iid)
            self.tree.delete(iid)

    def _clear(self) -> None:
        if self._is_running():
            return
        self.queue_paths.clear()
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.overall_bar["value"] = 0
        self.overall_label.configure(text="Idle")

    def _toggle_log(self) -> None:
        if self._log_visible.get():
            self.log_frame.pack(fill=tk.BOTH, expand=True, after=self.log_toggle_btn.master)
            self.root.geometry("")  # let it resize naturally
        else:
            self.log_frame.pack_forget()

    def _clear_log(self) -> None:
        self.log_text.delete("1.0", tk.END)

    def _log_key_filter(self, event) -> str | None:
        """Allow copy/select-all shortcuts and navigation; block everything else."""
        # Cmd on macOS (0x8), Ctrl on Linux/Win (0x4)
        mod = event.state & (0x4 | 0x8)
        if mod and event.keysym.lower() in ("c", "a"):
            return None  # allow Cmd+C, Cmd+A
        # Allow navigation keys
        if event.keysym in (
            "Up", "Down", "Left", "Right", "Home", "End",
            "Prior", "Next", "Shift_L", "Shift_R",
        ):
            return None
        return "break"  # block typing/editing

    def _show_log_menu(self, event) -> None:
        self._log_menu.tk_popup(event.x_root, event.y_root)

    def _copy_log_selection(self) -> None:
        try:
            text = self.log_text.get(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _copy_log_all(self) -> None:
        text = self.log_text.get("1.0", tk.END).rstrip("\n")
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)

    def _select_log_all(self) -> None:
        self.log_text.tag_add(tk.SEL, "1.0", tk.END)
        self.log_text.mark_set(tk.INSERT, "1.0")
        self.log_text.see(tk.INSERT)

    def _log(self, message: str, level: str = "info") -> None:
        ts = time.strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{ts}] ", "timestamp")
        self.log_text.insert(tk.END, f"{message}\n", level)
        self.log_text.see(tk.END)

    def _start_convert(self) -> None:
        if self._is_running():
            return
        if not self.queue_paths:
            messagebox.showwarning("Empty queue", "Add files or a folder first.")
            return
        try:
            workers = max(1, int(self.threads_var.get()))
        except (tk.TclError, ValueError):
            workers = CPU_COUNT
        self.cancel_event.clear()
        self.skip_events = {p: threading.Event() for p in self.queue_paths}
        self.progress_by_path = {p: 0.0 for p in self.queue_paths}
        self.completed_count = 0

        self.convert_btn.configure(state=tk.DISABLED)
        self.threads_spin.configure(state=tk.DISABLED)
        self.cancel_btn.configure(state=tk.NORMAL)
        self.skip_btn.configure(state=tk.NORMAL)
        self.overall_bar["value"] = 0
        start_msg = f"Starting — {len(self.queue_paths)} file(s) across {workers} thread(s)"
        self.overall_label.configure(text=start_msg)
        self._log(start_msg, "info")
        for path in self.queue_paths:
            if self.tree.exists(path):
                self.tree.item(
                    path,
                    values=(
                        STATUS_LABELS["pending"],
                        "",
                        _display_name(path),
                        os.path.dirname(path),
                        "",
                    ),
                    tags=(),
                )
        files = list(self.queue_paths)
        self.worker_thread = threading.Thread(
            target=_worker,
            args=(files, workers, self.msg_q, self.cancel_event, self.skip_events),
            daemon=True,
        )
        self.worker_thread.start()

    def _cancel(self) -> None:
        self.cancel_event.set()
        for event in self.skip_events.values():
            event.set()
        self.cancel_btn.configure(state=tk.DISABLED)
        self.skip_btn.configure(state=tk.DISABLED)
        self.overall_label.configure(text="Cancelling…")

    def _skip_selected(self) -> None:
        if not self._is_running():
            return
        selected = [iid for iid in self.tree.selection() if iid in self.skip_events]
        for path in selected:
            self.skip_events[path].set()

    def _is_running(self) -> bool:
        return self.worker_thread is not None and self.worker_thread.is_alive()

    def _schedule_poll(self) -> None:
        self.root.after(50, self._poll)

    def _poll(self) -> None:
        try:
            while True:
                msg, payload = self.msg_q.get_nowait()
                self._handle(msg, payload)
        except queue.Empty:
            pass
        self._schedule_poll()

    def _set_row(
        self,
        path: str,
        status_text: str,
        tag: str = "",
        progress_text: str = "",
        error_text: str = "",
    ) -> None:
        if not self.tree.exists(path):
            return
        current = self.tree.item(path, "values")
        self.tree.item(
            path,
            values=(
                status_text,
                progress_text if progress_text else (current[1] if len(current) > 1 else ""),
                _display_name(path),
                os.path.dirname(path),
                error_text if error_text else (current[4] if len(current) > 4 else ""),
            ),
            tags=(tag,) if tag else (),
        )

    def _refresh_overall(self) -> None:
        total = len(self.queue_paths)
        if total == 0:
            self.overall_bar["value"] = 0
            return
        agg = sum(self.progress_by_path.get(p, 0.0) for p in self.queue_paths)
        self.overall_bar["value"] = agg / total
        in_flight = sum(
            1 for pct in self.progress_by_path.values() if 0.0 < pct < 100.0
        )
        self.overall_label.configure(
            text=f"{self.completed_count}/{total} done — {in_flight} in flight"
        )

    def _handle(self, msg: str, payload) -> None:
        if msg == "file_start":
            path = payload["path"]
            self.progress_by_path[path] = 0.01
            self._set_row(path, "Converting", "converting", progress_text="0%")
            self.tree.see(path)
            self._log(f"START  {_display_name(path)}", "info")
            self._refresh_overall()
        elif msg == "file_progress":
            path = payload["path"]
            pct = float(payload["pct"])
            self.progress_by_path[path] = max(self.progress_by_path.get(path, 0.0), pct)
            self._set_row(path, "Converting", "converting", progress_text=f"{int(pct)}%")
            if int(pct) % 25 == 0 and int(pct) > 0:
                self._log(f"  {_display_name(path)} — {int(pct)}%", "progress")
            self._refresh_overall()
        elif msg == "file_done":
            path = payload["path"]
            status = payload["status"]
            tag = {
                "ok": "ok",
                "skipped": "skipped",
                "skipped_user": "skipped",
                "no_audio": "skipped",
                "cancelled": "skipped",
                "failed": "failed",
            }.get(status, "")
            pct_text = "100%" if status == "ok" else ""
            self._set_row(
                path,
                STATUS_LABELS.get(status, str(status)),
                tag,
                progress_text=pct_text,
            )
            self.progress_by_path[path] = 100.0
            self.completed_count += 1
            level = {"ok": "success", "failed": "error"}.get(status, "warn")
            label = STATUS_LABELS.get(status, status)
            self._log(f"DONE   {_display_name(path)} — {label}", level)
            self._refresh_overall()
        elif msg == "retry":
            path = payload["path"]
            attempt = payload["attempt"]
            error = payload.get("error", "")
            short_err = (error[:50] + "...") if len(error) > 50 else error
            self._set_row(
                path,
                f"Retry {attempt}/{MAX_RETRIES}",
                "skipped",
                error_text=short_err,
            )
            self._log(
                f"RETRY  {_display_name(path)} attempt {attempt}/{MAX_RETRIES}: {short_err}",
                "warn",
            )
        elif msg == "error_detail":
            path = payload["path"]
            error = payload.get("error", "unknown")
            short_err = (error[:80] + "...") if len(error) > 80 else error
            self._set_row(
                path,
                STATUS_LABELS["failed"],
                "failed",
                error_text=short_err,
            )
            self._log(
                f"ERROR  {_display_name(path)}: {error}",
                "error",
            )
        elif msg == "done":
            self.convert_btn.configure(state=tk.NORMAL)
            self.cancel_btn.configure(state=tk.DISABLED)
            self.skip_btn.configure(state=tk.DISABLED)
            self.threads_spin.configure(state=tk.NORMAL)
            if payload["cancelled"]:
                self.overall_label.configure(
                    text=f"Cancelled — {payload['completed']}/{payload['total']} processed"
                )
                self._log(
                    f"CANCELLED — {payload['completed']}/{payload['total']} processed",
                    "warn",
                )
            else:
                self.overall_bar["value"] = 100
                self.overall_label.configure(
                    text=f"Done — {payload['completed']}/{payload['total']} processed"
                )
                self._log(
                    f"ALL DONE — {payload['completed']}/{payload['total']} processed",
                    "success",
                )

    def _quit(self) -> None:
        if self._is_running():
            if not messagebox.askokcancel(
                "Conversion in progress",
                "A conversion is running. Cancel and quit?",
            ):
                return
            self.cancel_event.set()
            for event in self.skip_events.values():
                event.set()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
