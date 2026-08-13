"""PySide6 GUI for downloading Suno songs as MP3."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from string import Template

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QDate, QSettings, QThread, Signal
from PySide6.QtGui import QCloseEvent, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from download_song_mp3 import (
    DEFAULT_MP3_BITRATE_KBPS,
    BulkMediaItem,
    ClipRange,
    HookDownloadFormat,
    HookMetadata,
    PlaylistMetadata,
    SongMetadata,
    bearer_token_is_expired,
    clip_from_optional_strings,
    download_bulk_items,
    download_hook,
    download_playlist_mp3,
    download_song_mp3,
    fetch_private_library,
    fetch_user_hooks,
    fetch_user_profile_clips,
    hook_metadata_to_bulk_item,
    mask_bearer_token,
    normalize_bearer_token,
    parse_suno_input,
    song_metadata_to_bulk_item,
)

SETTINGS_ORG = "suno-scraper"
SETTINGS_APP = "download-gui"
BEARER_TOKEN_HELP_PATH = Path(__file__).resolve().parent / "docs" / "bearer_token.md"

# Custom stylesheet + group boxes don't get usable Qt defaults; these match the current look.
MARGIN_WINDOW = 18
MARGIN_TAB = 14
PADDING_SECTION = 14
SPACING_SECTION = 14
SPACING_INNER = 6
SPACING_ROW = 12
BULK_DATE_ANY_FROM = QDate(2000, 1, 1)
BULK_FILTER_CONTROL_HEIGHT = 34


def default_download_dir() -> Path:
    home = Path.home()
    downloads = home / "Downloads"
    return downloads if downloads.is_dir() else home


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hintLabel")
    label.setWordWrap(True)
    label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
    return label


def _tab_layout() -> tuple[QWidget, QVBoxLayout]:
    tab = QWidget()
    layout = QVBoxLayout(tab)
    layout.setSpacing(SPACING_SECTION)
    layout.setContentsMargins(MARGIN_TAB, MARGIN_TAB, MARGIN_TAB, MARGIN_TAB)
    return tab, layout


class Section(QWidget):
    """Rounded panel with its title centered on the top border."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._frame = QFrame(self)
        self._frame.setObjectName("sectionFrame")
        self._title = QLabel(title, self)
        self._title.setObjectName("sectionTitle")
        self._title.setAutoFillBackground(True)
        self.body = QVBoxLayout(self._frame)
        self.body.setContentsMargins(
            PADDING_SECTION, PADDING_SECTION, PADDING_SECTION, PADDING_SECTION
        )
        self.body.setSpacing(SPACING_INNER)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        # Styled line-edit height is only known after polish; relayout so rows
        # are tall enough to vertically center labels with the textboxes.
        self.updateGeometry()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._title.adjustSize()
        title_h = self._title.height()
        self._title.move(16, 0)
        self._frame.setGeometry(0, title_h // 2, self.width(), self.height() - title_h // 2)
        self._title.raise_()
        if event.oldSize().width() != event.size().width():
            self.updateGeometry()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        extra = max(self._title.sizeHint().height(), 1) // 2
        return self._frame_height(width) + extra

    def _frame_height(self, width: int) -> int:
        layout = self._frame.layout()
        if layout is None or width <= 0:
            return self._frame.sizeHint().height()
        if layout.hasHeightForWidth():
            return layout.heightForWidth(width)
        return layout.sizeHint().height()

    def sizeHint(self) -> QSize:
        frame_hint = self._frame.sizeHint()
        width = self.width() if self.width() > 0 else frame_hint.width()
        return QSize(
            max(frame_hint.width(), self._title.sizeHint().width() + 32),
            self.heightForWidth(width),
        )

    def minimumSizeHint(self) -> QSize:
        width = self.width() if self.width() > 0 else self._frame.minimumSizeHint().width()
        return QSize(self._frame.minimumSizeHint().width(), self.heightForWidth(width))


def _add_group(
    parent: QVBoxLayout,
    title: str,
    *,
    stretch: int = 0,
) -> QVBoxLayout:
    section = Section(title)
    if stretch > 0:
        section.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Expanding,
        )
    parent.addWidget(section, stretch)
    return section.body


def _row(*widgets: QWidget, stretch: tuple[int, ...] = ()) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(SPACING_ROW)
    for index, widget in enumerate(widgets):
        row.addWidget(
            widget,
            1 if index in stretch else 0,
            Qt.AlignmentFlag.AlignVCenter,
        )
    return row


def _set_bulk_filter_control_height(widget: QWidget) -> None:
    widget.setFixedHeight(BULK_FILTER_CONTROL_HEIGHT)
    widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)


class _BulkDateCalendarFilter(QObject):
    """Open the calendar near today when the field still means \"Any\"."""

    def __init__(self, date_edit: QDateEdit, any_date: QDate) -> None:
        super().__init__(date_edit)
        self._date_edit = date_edit
        self._any_date = any_date
        date_edit.calendarWidget().installEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Show and self._date_edit.date() == self._any_date:
            today = QDate.currentDate()
            calendar = self._date_edit.calendarWidget()
            calendar.setCurrentPage(today.year(), today.month())
        return super().eventFilter(obj, event)


def _configure_bulk_date_edit(date_edit: QDateEdit, *, any_date: QDate) -> None:
    date_edit.setCalendarPopup(True)
    date_edit.setDisplayFormat("yyyy-MM-dd")
    date_edit.setSpecialValueText("Any")
    date_edit.setMinimumDate(any_date)
    date_edit.setDate(any_date)
    date_edit.setObjectName("bulkFilterControl")
    _set_bulk_filter_control_height(date_edit)
    _BulkDateCalendarFilter(date_edit, any_date)


@dataclass
class DownloadResult:
    saved_to: str
    song: SongMetadata | None = None
    playlist: PlaylistMetadata | None = None
    hook: HookMetadata | None = None
    paths: list[Path] = field(default_factory=list)
    failures: list[tuple[SongMetadata, str]] = field(default_factory=list)
    bulk_failures: list[tuple[BulkMediaItem, str]] = field(default_factory=list)


def load_bearer_token_help_markdown() -> str:
    if BEARER_TOKEN_HELP_PATH.is_file():
        return BEARER_TOKEN_HELP_PATH.read_text(encoding="utf-8")
    return (
        "## Getting Your Bearer Token\n\n"
        "See docs/bearer_token.md in the project folder for instructions."
    )


def _parse_item_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_item_date(value: str | None) -> str:
    parsed = _parse_item_date(value)
    if parsed is None:
        return ""
    return parsed.strftime("%Y-%m-%d")


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


class BearerTokenHelpDialog(QDialog):
    """Modeless dialog showing bearer token instructions."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("How to get your bearer token")
        self.setMinimumSize(520, 420)
        layout = QVBoxLayout(self)
        browser = QTextBrowser(self)
        browser.setOpenExternalLinks(True)
        browser.setMarkdown(load_bearer_token_help_markdown())
        layout.addWidget(browser)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.close)
        layout.addWidget(close_button)


class ProfileLoadWorker(QThread):
    status = Signal(str)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        *,
        mode: str,
        handle: str = "",
        token: str = "",
        load_songs: bool = True,
        load_hooks: bool = True,
    ) -> None:
        super().__init__()
        self.mode = mode
        self.handle = handle
        self.token = token
        self.load_songs = load_songs
        self.load_hooks = load_hooks

    def run(self) -> None:
        try:
            items: list[BulkMediaItem] = []
            if self.mode == "library":
                self.status.emit("Loading your library…")

                def on_count(count: int) -> None:
                    self.status.emit(f"Loading your library… {count} songs")

                songs = fetch_private_library(self.token, on_progress=on_count)
                items.extend(song_metadata_to_bulk_item(s) for s in songs)
                self.finished_ok.emit(items)
                return

            handle = self.handle.strip().lstrip("@")
            if not handle:
                raise ValueError("Enter a username to load.")

            if self.load_songs:
                def on_page(page: int, total: int) -> None:
                    self.status.emit(f"Loading songs… page {page}")

                profile = fetch_user_profile_clips(handle, on_page=on_page)
                for clip_meta in profile.clips:
                    item = song_metadata_to_bulk_item(clip_meta)
                    items.append(item)

            if self.load_hooks:
                self.status.emit("Loading hooks…")
                hooks = fetch_user_hooks(handle)
                items.extend(hook_metadata_to_bulk_item(h) for h in hooks)

            self.finished_ok.emit(items)
        except Exception as exc:
            self.failed.emit(str(exc))


class BulkDownloadWorker(QThread):
    progress = Signal(int, int)
    status = Signal(str)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        items: list[BulkMediaItem],
        output_dir: Path,
        *,
        folder_name: str,
        use_subfolder: bool,
        hook_format: HookDownloadFormat,
        attach_metadata: bool,
    ) -> None:
        super().__init__()
        self.items = items
        self.output_dir = output_dir
        self.folder_name = folder_name
        self.use_subfolder = use_subfolder
        self.hook_format = hook_format
        self.attach_metadata = attach_metadata

    def run(self) -> None:
        try:
            def on_progress(done: int, total: int | None) -> None:
                self.progress.emit(done, total or 0)

            def on_item(current: int, total: int, title: str) -> None:
                self.status.emit(f"Downloading {current}/{total}: {title}")

            paths, failures = download_bulk_items(
                self.items,
                self.output_dir,
                folder_name=self.folder_name,
                use_subfolder=self.use_subfolder,
                hook_format=self.hook_format,
                progress=on_progress,
                on_item=on_item,
                attach_metadata=self.attach_metadata,
            )
            saved_to = str(paths[0].parent) if paths else str(self.output_dir)
            self.finished_ok.emit(
                DownloadResult(saved_to, paths=paths, bulk_failures=failures)
            )
        except Exception as exc:
            self.failed.emit(str(exc))


class DownloadWorker(QThread):
    progress = Signal(int, int)
    status = Signal(str)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        url_input: str,
        output_dir: Path,
        *,
        use_subfolder: bool,
        clip: ClipRange | None = None,
        attach_metadata: bool = True,
        hook_format: HookDownloadFormat = "both",
    ) -> None:
        super().__init__()
        self.url_input = url_input
        self.output_dir = output_dir
        self.use_subfolder = use_subfolder
        self.clip = clip
        self.attach_metadata = attach_metadata
        self.hook_format = hook_format

    def run(self) -> None:
        try:
            kind, resource_id = parse_suno_input(self.url_input)

            def on_progress(done: int, total: int | None) -> None:
                self.progress.emit(done, total or 0)

            if kind == "hook":
                paths, metadata = download_hook(
                    resource_id,
                    self.output_dir,
                    hook_format=self.hook_format,
                    progress=on_progress,
                    attach_metadata=self.attach_metadata,
                )
                saved_to = str(paths[0].parent) if paths else str(self.output_dir)
                self.finished_ok.emit(
                    DownloadResult(saved_to, hook=metadata, paths=paths)
                )
                return

            if kind == "song":
                path, metadata = download_song_mp3(
                    resource_id,
                    self.output_dir,
                    progress=on_progress,
                    clip=self.clip,
                    attach_metadata=self.attach_metadata,
                )
                self.finished_ok.emit(DownloadResult(str(path), song=metadata))
                return

            def on_track(current: int, total: int, title: str) -> None:
                self.status.emit(f"Downloading {current}/{total}: {title}")

            paths, failures, playlist = download_playlist_mp3(
                resource_id,
                self.output_dir,
                use_subfolder=self.use_subfolder,
                progress=on_progress,
                on_track=on_track,
                attach_metadata=self.attach_metadata,
            )
            saved_to = str(paths[0].parent) if paths else str(self.output_dir)
            self.finished_ok.emit(
                DownloadResult(saved_to, playlist=playlist, paths=paths, failures=failures)
            )
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self.worker: DownloadWorker | None = None
        self.profile_worker: ProfileLoadWorker | None = None
        self.bulk_worker: BulkDownloadWorker | None = None
        self.token_help_dialog: BearerTokenHelpDialog | None = None
        self.bulk_items: list[BulkMediaItem] = []
        self.bulk_selected_ids: set[str] = set()
        self.bulk_folder_name = "bulk-download"
        self.detected_bitrate = DEFAULT_MP3_BITRATE_KBPS

        self.setWindowTitle("Suno MP3 Downloader")
        self.setMinimumSize(640, 560)
        # Apply the stylesheet before building widgets so layout sizeHints include
        # styled padding/min-height (otherwise labels layout against unstyled heights).
        self._apply_theme(self._settings_bool("dark_mode"))
        self._build_ui()
        self._load_settings()
        self._refresh_format_options()
        self._on_url_changed()

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(MARGIN_WINDOW, MARGIN_WINDOW, MARGIN_WINDOW, MARGIN_WINDOW)

        self.tabs = QTabWidget()
        self.tabs.tabBar().setDrawBase(False)
        self.tabs.tabBar().setExpanding(False)
        self.tabs.addTab(self._build_download_tab(), "Download")
        self.tabs.addTab(self._build_bulk_tab(), "Bulk Download")
        self.tabs.addTab(self._build_options_tab(), "Options")
        outer.addWidget(self.tabs)

    def _build_download_tab(self) -> QWidget:
        tab, layout = _tab_layout()

        fields = QWidget()
        fields_layout = QVBoxLayout(fields)
        fields_layout.setContentsMargins(0, 0, 0, 0)
        fields_layout.setSpacing(SPACING_SECTION)

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText(
            "https://suno.com/song/... or playlist/... or hook/..."
        )
        self.url_input.textChanged.connect(self._on_url_changed)
        source = _add_group(fields_layout, "Source")
        source.addWidget(self.url_input)
        source.addWidget(
            _hint(
                "Paste a Suno song, playlist, or hook URL, or a 36-character ID.\n"
                "Song: https://suno.com/song/094ac41f-93f5-4f12-a93b-2c74940b69b7\n"
                "Playlist: https://suno.com/playlist/7a8259af-549e-47f3-874e-6d0f1d76e272\n"
                "Hook: https://suno.com/hook/b8aea5cf-fb9b-45ef-870a-b213e9087d3c"
            )
        )

        self.clip_start_input = QLineEdit()
        self.clip_start_input.setPlaceholderText("1:30 or 90")
        self.clip_end_input = QLineEdit()
        self.clip_end_input.setPlaceholderText("2:00 or 120")
        clip = _add_group(fields_layout, "Clip (optional)")
        clip.addLayout(
            _row(
                QLabel("Start"),
                self.clip_start_input,
                QLabel("End"),
                self.clip_end_input,
                stretch=(1, 3),
            )
        )
        clip.addWidget(
            _hint(
                "Leave both blank for the full song. Set either start or end to clip. "
                "Accepts MM:SS or seconds"
            )
        )

        self.folder_input = QLineEdit()
        self.browse_button = QPushButton("Browse…")
        self.browse_button.setObjectName("browseButton")
        self.browse_button.clicked.connect(self._choose_folder)
        output = _add_group(fields_layout, "Output")
        output.addLayout(
            _row(QLabel("Download folder"), self.folder_input, self.browse_button, stretch=(1,))
        )

        self._download_fields = fields
        layout.addWidget(fields)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setFormat("%p%")
        layout.addWidget(self.progress)

        self.status_label = QLabel("Ready to download.")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.download_button = QPushButton("Download MP3")
        self.download_button.clicked.connect(self._start_download)
        layout.addWidget(self.download_button)

        layout.addStretch()
        return tab

    def _build_bulk_tab(self) -> QWidget:
        tab, layout = _tab_layout()
        layout.setSpacing(SPACING_INNER)

        self.bulk_username_input = QLineEdit()
        self.bulk_username_input.setPlaceholderText("Suno @username (without @)")
        self.bulk_username_input.textChanged.connect(self._save_settings)
        self.bulk_load_songs_checkbox = QCheckBox("Songs")
        self.bulk_load_songs_checkbox.setChecked(True)
        self.bulk_load_hooks_checkbox = QCheckBox("Hooks")
        self.bulk_load_hooks_checkbox.setChecked(True)
        self.bulk_load_profile_button = QPushButton("Load profile")
        self.bulk_load_profile_button.setFixedWidth(104)
        self.bulk_load_profile_button.clicked.connect(self._start_profile_load)
        profile_row = QHBoxLayout()
        profile_row.setContentsMargins(0, 0, 0, 0)
        profile_row.setSpacing(SPACING_ROW)
        profile_row.addWidget(self.bulk_username_input, 1)
        profile_row.addWidget(self.bulk_load_songs_checkbox, 0, Qt.AlignmentFlag.AlignVCenter)
        profile_row.addWidget(self.bulk_load_hooks_checkbox, 0, Qt.AlignmentFlag.AlignVCenter)
        profile_row.addWidget(self.bulk_load_profile_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(profile_row)

        self.bearer_token_input = QLineEdit()
        self.bearer_token_input.setPlaceholderText("Bearer token (optional, for private library)")
        self.bearer_token_input.textChanged.connect(self._on_bearer_token_changed)
        self.bearer_token_help_button = QPushButton("How to get token")
        self.bearer_token_help_button.setObjectName("browseButton")
        self.bearer_token_help_button.setToolTip("Open instructions for copying your Suno bearer token")
        self.bearer_token_help_button.clicked.connect(self._show_bearer_token_help)
        self.bulk_load_library_button = QPushButton("Load my library")
        self.bulk_load_library_button.setMinimumWidth(
            self.bulk_load_library_button.fontMetrics().horizontalAdvance("Load my library") + 36
        )
        self.bulk_load_library_button.clicked.connect(self._start_library_load)
        library_row = QHBoxLayout()
        library_row.setContentsMargins(0, 0, 0, 0)
        library_row.setSpacing(SPACING_ROW)
        library_row.addWidget(self.bearer_token_input, 1)
        library_row.addWidget(self.bearer_token_help_button, 0, Qt.AlignmentFlag.AlignVCenter)
        library_row.addWidget(self.bulk_load_library_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(library_row)

        self.bearer_token_status_label = QLabel(
            "Optional — needed for private/unpublished songs."
        )
        self.bearer_token_status_label.setObjectName("hintLabel")
        self.bearer_token_status_label.setWordWrap(True)
        layout.addWidget(self.bearer_token_status_label)

        results = _add_group(layout, "Filter & Results", stretch=1)
        filter_row = QHBoxLayout()
        filter_row.setSpacing(SPACING_INNER)
        filter_row.setContentsMargins(0, 0, 0, 0)
        self.bulk_title_filter = QLineEdit()
        self.bulk_title_filter.setPlaceholderText("Title…")
        self.bulk_title_filter.setObjectName("bulkFilterControl")
        _set_bulk_filter_control_height(self.bulk_title_filter)
        self.bulk_title_filter.textChanged.connect(self._refresh_bulk_table)
        self.bulk_date_from = QDateEdit()
        _configure_bulk_date_edit(self.bulk_date_from, any_date=BULK_DATE_ANY_FROM)
        self.bulk_date_from.setMaximumWidth(118)
        self.bulk_date_to = QDateEdit()
        _configure_bulk_date_edit(self.bulk_date_to, any_date=QDate.currentDate())
        self.bulk_date_to.setMaximumWidth(118)
        self.bulk_sort_combo = QComboBox()
        self.bulk_sort_combo.addItem("Newest", "date_desc")
        self.bulk_sort_combo.addItem("Views", "views_desc")
        self.bulk_sort_combo.addItem("Likes", "likes_desc")
        self.bulk_sort_combo.setObjectName("bulkFilterControl")
        self.bulk_sort_combo.setMaximumWidth(100)
        _set_bulk_filter_control_height(self.bulk_sort_combo)
        self.bulk_sort_combo.currentIndexChanged.connect(self._refresh_bulk_table)
        self.bulk_select_filtered_button = QPushButton("Select all")
        self.bulk_select_filtered_button.setObjectName("bulkFilterButton")
        self.bulk_select_filtered_button.setFixedWidth(78)
        self.bulk_select_filtered_button.clicked.connect(self._select_all_filtered)
        self.bulk_unselect_all_button = QPushButton("Unselect all")
        self.bulk_unselect_all_button.setObjectName("bulkFilterButton")
        self.bulk_unselect_all_button.setFixedWidth(78)
        self.bulk_unselect_all_button.clicked.connect(self._clear_bulk_selection)
        _set_bulk_filter_control_height(self.bulk_select_filtered_button)
        _set_bulk_filter_control_height(self.bulk_unselect_all_button)
        self.bulk_date_from.dateChanged.connect(self._refresh_bulk_table)
        self.bulk_date_to.dateChanged.connect(self._refresh_bulk_table)
        filter_fields = (
            (QLabel("Title"), self.bulk_title_filter, 1),
            (QLabel("From"), self.bulk_date_from, 0),
            (QLabel("To"), self.bulk_date_to, 0),
            (QLabel("Sort"), self.bulk_sort_combo, 0),
        )
        for label, widget, stretch in filter_fields:
            label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
            label.setFixedHeight(BULK_FILTER_CONTROL_HEIGHT)
            filter_row.addWidget(label)
            filter_row.addWidget(widget, stretch, Qt.AlignmentFlag.AlignVCenter)
        filter_row.addWidget(self.bulk_select_filtered_button, 0, Qt.AlignmentFlag.AlignVCenter)
        filter_row.addWidget(self.bulk_unselect_all_button, 0, Qt.AlignmentFlag.AlignVCenter)
        filter_row.addStretch()
        results.addLayout(filter_row)

        self.bulk_counts_label = QLabel("0 selected · 0 shown · 0 total")
        self.bulk_counts_label.setObjectName("statusLabel")
        results.addWidget(self.bulk_counts_label)

        self.bulk_table = QTableWidget(0, 7)
        self.bulk_table.setHorizontalHeaderLabels(
            ["", "Type", "Title", "Date", "Length", "Views", "Likes"]
        )
        self.bulk_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.bulk_table.setColumnWidth(4, 56)
        self.bulk_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.bulk_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.bulk_table.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.bulk_table.setMinimumHeight(280)
        results.addWidget(self.bulk_table, 1)

        self.bulk_folder_input = QLineEdit()
        self.bulk_folder_input.textChanged.connect(self._save_settings)
        self.bulk_browse_button = QPushButton("Browse…")
        self.bulk_browse_button.setObjectName("browseButton")
        self.bulk_browse_button.clicked.connect(self._choose_bulk_folder)
        folder_row = QHBoxLayout()
        folder_row.setContentsMargins(0, 0, 0, 0)
        folder_row.setSpacing(SPACING_ROW)
        folder_row.addWidget(QLabel("Download folder"))
        folder_row.addWidget(self.bulk_folder_input, 1)
        folder_row.addWidget(self.bulk_browse_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(folder_row)

        self.bulk_progress = QProgressBar()
        self.bulk_progress.setRange(0, 100)
        self.bulk_progress.setFormat("%p%")
        layout.addWidget(self.bulk_progress)

        self.bulk_status_label = QLabel("Load a profile or library to begin.")
        self.bulk_status_label.setObjectName("statusLabel")
        self.bulk_status_label.setWordWrap(True)
        layout.addWidget(self.bulk_status_label)

        self.bulk_download_button = QPushButton("Download selected")
        self.bulk_download_button.clicked.connect(self._start_bulk_download)
        layout.addWidget(self.bulk_download_button)

        self._bulk_fields = tab
        self.setMinimumSize(720, 640)
        return tab

    def _build_options_tab(self) -> QWidget:
        tab, layout = _tab_layout()

        fmt = _add_group(layout, "Format")
        self.format_combo = QComboBox()
        fmt.addWidget(self.format_combo)
        fmt.addWidget(
            _hint("Only MP3 for now — this is the best-quality audio Suno streams on the CDN.")
        )

        playlists = _add_group(layout, "Playlists")
        self.playlist_subfolder_checkbox = QCheckBox(
            "Save playlists in a subfolder named after the playlist"
        )
        self.playlist_subfolder_checkbox.toggled.connect(self._save_settings)
        playlists.addWidget(self.playlist_subfolder_checkbox)

        hooks = _add_group(layout, "Hooks")
        self.hook_format_combo = QComboBox()
        self.hook_format_combo.addItem("Both (MP4 + MP3)", "both")
        self.hook_format_combo.addItem("MP4 only", "mp4")
        self.hook_format_combo.addItem("MP3 only", "mp3")
        self.hook_format_combo.currentIndexChanged.connect(self._save_settings)
        hooks.addWidget(self.hook_format_combo)
        hooks.addWidget(_hint("Default format when downloading hooks (bulk or single URL)."))

        metadata = _add_group(layout, "Metadata")
        self.attach_metadata_checkbox = QCheckBox(
            "Download and attach metadata (title, author, cover art, styles, etc.)"
        )
        self.attach_metadata_checkbox.toggled.connect(self._save_settings)
        metadata.addWidget(self.attach_metadata_checkbox)

        appearance = _add_group(layout, "Appearance")
        self.dark_mode_checkbox = QCheckBox("Dark mode")
        self.dark_mode_checkbox.toggled.connect(self._on_dark_mode_toggled)
        appearance.addWidget(self.dark_mode_checkbox)

        layout.addStretch()
        return tab

    def _settings_bool(self, key: str, default: bool = False) -> bool:
        value = self.settings.value(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes")
        return bool(value)

    def _set_checkbox(self, checkbox: QCheckBox, key: str, default: bool) -> None:
        checkbox.blockSignals(True)
        checkbox.setChecked(self._settings_bool(key, default))
        checkbox.blockSignals(False)

    def _load_settings(self) -> None:
        dark_mode = self._settings_bool("dark_mode")
        folder = self.settings.value("output_dir", str(default_download_dir()))
        self.folder_input.setText(str(folder))
        bulk_folder = self.settings.value("bulk_output_dir", "", type=str).strip()
        if not bulk_folder:
            bulk_folder = str(folder)
        self.bulk_folder_input.blockSignals(True)
        self.bulk_folder_input.setText(bulk_folder)
        self.bulk_folder_input.blockSignals(False)
        self.bulk_username_input.blockSignals(True)
        self.bulk_username_input.setText(
            self.settings.value("bulk_username", "", type=str)
        )
        self.bulk_username_input.blockSignals(False)
        self.dark_mode_checkbox.blockSignals(True)
        self.dark_mode_checkbox.setChecked(dark_mode)
        self.dark_mode_checkbox.blockSignals(False)
        self._set_checkbox(self.playlist_subfolder_checkbox, "playlist_subfolder", True)
        self._set_checkbox(self.attach_metadata_checkbox, "attach_metadata", True)
        hook_format = self.settings.value("hook_download_format", "both", type=str)
        self.hook_format_combo.blockSignals(True)
        index = self.hook_format_combo.findData(hook_format)
        if index >= 0:
            self.hook_format_combo.setCurrentIndex(index)
        self.hook_format_combo.blockSignals(False)
        self.bearer_token_input.blockSignals(True)
        self.bearer_token_input.setText(self.settings.value("bearer_token", "", type=str))
        self.bearer_token_input.blockSignals(False)
        self._apply_theme(dark_mode)
        self._update_bearer_token_status()
        self._update_library_button_state()

    def _save_settings(self) -> None:
        self.settings.setValue("output_dir", self.folder_input.text().strip())
        self.settings.setValue("bulk_output_dir", self.bulk_folder_input.text().strip())
        self.settings.setValue(
            "bulk_username",
            self.bulk_username_input.text().strip().lstrip("@"),
        )
        self.settings.setValue("dark_mode", self.dark_mode_checkbox.isChecked())
        self.settings.setValue("playlist_subfolder", self.playlist_subfolder_checkbox.isChecked())
        self.settings.setValue("attach_metadata", self.attach_metadata_checkbox.isChecked())
        self.settings.setValue(
            "hook_download_format",
            self.hook_format_combo.currentData() or "both",
        )
        token_text = self.bearer_token_input.text().strip()
        if token_text:
            try:
                self.settings.setValue("bearer_token", normalize_bearer_token(token_text))
            except ValueError:
                self.settings.setValue("bearer_token", token_text)
        else:
            self.settings.setValue("bearer_token", "")
        self.settings.sync()

    def _refresh_format_options(self) -> None:
        self.format_combo.clear()
        self.format_combo.addItem(f"MP3 ({self.detected_bitrate} kbps)", "mp3")
        self.format_combo.setEnabled(False)

    def _hook_format(self) -> HookDownloadFormat:
        value = self.hook_format_combo.currentData()
        if value in ("both", "mp4", "mp3"):
            return value
        return "both"

    def _stored_bearer_token(self) -> str:
        text = self.bearer_token_input.text().strip()
        if not text:
            return ""
        try:
            return normalize_bearer_token(text)
        except ValueError:
            return text

    def _on_bearer_token_changed(self) -> None:
        self._update_bearer_token_status()
        self._update_library_button_state()

    def _show_bearer_token_help(self) -> None:
        if self.token_help_dialog is None:
            self.token_help_dialog = BearerTokenHelpDialog(self)
        self.token_help_dialog.show()
        self.token_help_dialog.raise_()
        self.token_help_dialog.activateWindow()

    def _update_bearer_token_status(self) -> None:
        text = self.bearer_token_input.text().strip()
        if not text:
            self.bearer_token_status_label.setText(
                "Optional — needed for private/unpublished songs."
            )
            return
        try:
            token = normalize_bearer_token(text)
        except ValueError as exc:
            self.bearer_token_status_label.setText(str(exc))
            return
        if bearer_token_is_expired(token):
            self.bearer_token_status_label.setText(
                f"Token {mask_bearer_token(token)} looks expired — get a fresh one."
            )
            return
        self.bearer_token_status_label.setText(
            f"Token {mask_bearer_token(token)} looks valid."
        )

    def _update_library_button_state(self) -> None:
        has_token = bool(self._stored_bearer_token())
        self.bulk_load_library_button.setEnabled(has_token)

    def _filtered_bulk_items(self) -> list[BulkMediaItem]:
        title_query = self.bulk_title_filter.text().strip().lower()
        use_from = self.bulk_date_from.date() != BULK_DATE_ANY_FROM
        use_to = self.bulk_date_to.date() != QDate.currentDate()
        date_from = self.bulk_date_from.date().toPython() if use_from else None
        date_to = self.bulk_date_to.date().toPython() if use_to else None
        sort_key = self.bulk_sort_combo.currentData()

        filtered: list[BulkMediaItem] = []
        for item in self.bulk_items:
            if title_query and title_query not in item.title.lower():
                continue
            item_date = _parse_item_date(item.created_at)
            if date_from and item_date and item_date.date() < date_from:
                continue
            if date_to and item_date and item_date.date() > date_to:
                continue
            filtered.append(item)

        if sort_key == "views_desc":
            filtered.sort(key=lambda i: i.play_count or 0, reverse=True)
        elif sort_key == "likes_desc":
            filtered.sort(key=lambda i: i.upvote_count or 0, reverse=True)
        else:
            filtered.sort(
                key=lambda i: _parse_item_date(i.created_at) or datetime.min,
                reverse=True,
            )
        return filtered

    def _refresh_bulk_table(self) -> None:
        filtered = self._filtered_bulk_items()
        self.bulk_table.setRowCount(len(filtered))
        for row, item in enumerate(filtered):
            checkbox = QCheckBox()
            checkbox.setChecked(item.id in self.bulk_selected_ids)
            checkbox.stateChanged.connect(
                lambda _state, item_id=item.id, cb=checkbox: self._on_bulk_checkbox_changed(
                    item_id, cb.isChecked()
                )
            )
            self.bulk_table.setCellWidget(row, 0, checkbox)
            self.bulk_table.setItem(row, 1, QTableWidgetItem(item.kind.title()))
            self.bulk_table.setItem(row, 2, QTableWidgetItem(item.title))
            self.bulk_table.setItem(row, 3, QTableWidgetItem(_format_item_date(item.created_at)))
            self.bulk_table.setItem(row, 4, QTableWidgetItem(_format_duration(item.duration_sec)))
            views = "" if item.play_count is None else f"{item.play_count:,}"
            likes = "" if item.upvote_count is None else f"{item.upvote_count:,}"
            self.bulk_table.setItem(row, 5, QTableWidgetItem(views))
            self.bulk_table.setItem(row, 6, QTableWidgetItem(likes))
        self._update_bulk_counts_label(filtered)

    def _update_bulk_counts_label(self, filtered: list[BulkMediaItem] | None = None) -> None:
        if filtered is None:
            filtered = self._filtered_bulk_items()
        selected_filtered = sum(1 for item in filtered if item.id in self.bulk_selected_ids)
        self.bulk_counts_label.setText(
            f"{selected_filtered} selected · {len(filtered)} shown · {len(self.bulk_items)} total"
        )

    def _on_bulk_checkbox_changed(self, item_id: str, checked: bool) -> None:
        if checked:
            self.bulk_selected_ids.add(item_id)
        else:
            self.bulk_selected_ids.discard(item_id)
        self._update_bulk_counts_label()

    def _select_all_filtered(self) -> None:
        for item in self._filtered_bulk_items():
            self.bulk_selected_ids.add(item.id)
        self._refresh_bulk_table()

    def _clear_bulk_selection(self) -> None:
        self.bulk_selected_ids.clear()
        self._refresh_bulk_table()

    def _set_bulk_busy(self, busy: bool) -> None:
        self._bulk_fields.setEnabled(not busy)
        if not busy:
            self.bulk_progress.setValue(0)

    def _start_profile_load(self) -> None:
        handle = self.bulk_username_input.text().strip()
        if not handle:
            self._warn("Missing username", "Enter a Suno username to load.")
            return
        if not self.bulk_load_songs_checkbox.isChecked() and not self.bulk_load_hooks_checkbox.isChecked():
            self._warn("Nothing selected", "Enable Songs and/or Hooks to load.")
            return

        self._set_bulk_busy(True)
        self.bulk_status_label.setText("Loading profile…")
        self.bulk_folder_name = f"{handle.lstrip('@')} - Library"
        self.profile_worker = ProfileLoadWorker(
            mode="profile",
            handle=handle,
            load_songs=self.bulk_load_songs_checkbox.isChecked(),
            load_hooks=self.bulk_load_hooks_checkbox.isChecked(),
        )
        self.profile_worker.status.connect(self.bulk_status_label.setText)
        self.profile_worker.finished_ok.connect(self._on_profile_load_ok)
        self.profile_worker.failed.connect(self._on_profile_load_failed)
        self.profile_worker.start()

    def _start_library_load(self) -> None:
        token = self._stored_bearer_token()
        if not token:
            self._warn("Missing token", "Paste your bearer token above to load your library.")
            return

        self._set_bulk_busy(True)
        self.bulk_status_label.setText("Loading your library…")
        self.bulk_folder_name = "My Library"
        self.profile_worker = ProfileLoadWorker(mode="library", token=token, load_songs=True, load_hooks=False)
        self.profile_worker.status.connect(self.bulk_status_label.setText)
        self.profile_worker.finished_ok.connect(self._on_profile_load_ok)
        self.profile_worker.failed.connect(self._on_profile_load_failed)
        self.profile_worker.start()

    def _on_profile_load_ok(self, items: object) -> None:
        assert isinstance(items, list)
        self._set_bulk_busy(False)
        self.bulk_items = items
        self.bulk_selected_ids = {item.id for item in items}
        self._refresh_bulk_table()
        self.bulk_status_label.setText(f"Loaded {len(items)} items.")

    def _on_profile_load_failed(self, message: str) -> None:
        self._set_bulk_busy(False)
        self.bulk_status_label.setText("Load failed.")
        self._warn("Load failed", message)

    def _start_bulk_download(self) -> None:
        folder = self.bulk_folder_input.text().strip()
        if not folder:
            self._warn("Missing folder", "Choose a download folder.")
            return
        if not self.bulk_selected_ids:
            self._warn("Nothing selected", "Select at least one item to download.")
            return

        selected = [item for item in self.bulk_items if item.id in self.bulk_selected_ids]
        if not selected:
            self._warn("Nothing selected", "Select at least one item to download.")
            return

        output_dir = Path(folder)
        handle = self.bulk_username_input.text().strip().lstrip("@") or "download"
        use_subfolder = self.playlist_subfolder_checkbox.isChecked()
        song_items = [item for item in selected if item.kind == "song"]
        hook_items = [item for item in selected if item.kind == "hook"]

        batches: list[tuple[list[BulkMediaItem], str]] = []
        if song_items:
            song_folder = self.bulk_folder_name
            if song_folder in ("bulk-download", f"{handle} - Library"):
                song_folder = f"{handle} - Songs"
            batches.append((song_items, song_folder))
        if hook_items:
            batches.append((hook_items, f"{handle} - Hooks"))

        self._save_settings()
        self._set_bulk_busy(True)
        self.bulk_progress.setValue(0)
        self.bulk_status_label.setText("Starting download…")
        self._run_bulk_batches(output_dir, batches, use_subfolder, batch_index=0, all_paths=[])

    def _run_bulk_batches(
        self,
        output_dir: Path,
        batches: list[tuple[list[BulkMediaItem], str]],
        use_subfolder: bool,
        *,
        batch_index: int,
        all_paths: list[Path],
    ) -> None:
        if batch_index >= len(batches):
            self._set_bulk_busy(False)
            self.bulk_progress.setValue(100)
            saved_to = str(all_paths[0].parent) if all_paths else str(output_dir)
            self.bulk_status_label.setText(
                f"Bulk download complete — {len(all_paths)} files saved to {saved_to}"
            )
            return

        items, folder_name = batches[batch_index]

        def on_finished(result: object) -> None:
            assert isinstance(result, DownloadResult)
            all_paths.extend(result.paths)
            fail_note = f", {len(result.bulk_failures)} failed" if result.bulk_failures else ""
            self.bulk_status_label.setText(
                f"Finished {folder_name}{fail_note}. Continuing…"
            )
            self._run_bulk_batches(
                output_dir,
                batches,
                use_subfolder,
                batch_index=batch_index + 1,
                all_paths=all_paths,
            )

        self.bulk_worker = BulkDownloadWorker(
            items,
            output_dir,
            folder_name=folder_name,
            use_subfolder=use_subfolder,
            hook_format=self._hook_format(),
            attach_metadata=self.attach_metadata_checkbox.isChecked(),
        )
        self.bulk_worker.progress.connect(self._on_bulk_progress)
        self.bulk_worker.status.connect(self.bulk_status_label.setText)
        self.bulk_worker.finished_ok.connect(on_finished)
        self.bulk_worker.failed.connect(self._on_bulk_failed)
        self.bulk_worker.start()

    def _on_bulk_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.bulk_progress.setValue(min(100, done * 100 // total))

    def _on_bulk_failed(self, message: str) -> None:
        self._set_bulk_busy(False)
        self.bulk_progress.setValue(0)
        self.bulk_status_label.setText("Bulk download failed.")
        self._warn("Bulk download failed", message)

    def _apply_theme(self, dark: bool) -> None:
        self.setStyleSheet(build_stylesheet(dark))

    def _on_dark_mode_toggled(self, enabled: bool) -> None:
        self._apply_theme(enabled)
        self._save_settings()

    def _on_url_changed(self, _text: str = "") -> None:
        is_playlist = False
        is_hook = False
        text = self.url_input.text().strip()
        if text:
            try:
                kind, _ = parse_suno_input(text)
                is_playlist = kind == "playlist"
                is_hook = kind == "hook"
            except ValueError:
                pass

        disable_clip = is_playlist or is_hook
        self.clip_start_input.setEnabled(not disable_clip)
        self.clip_end_input.setEnabled(not disable_clip)
        if disable_clip:
            self.clip_start_input.clear()
            self.clip_end_input.clear()

    def _choose_folder(self) -> None:
        start_dir = self.folder_input.text().strip() or str(default_download_dir())
        chosen = QFileDialog.getExistingDirectory(self, "Choose download folder", start_dir)
        if chosen:
            self.folder_input.setText(chosen)
            self._save_settings()

    def _choose_bulk_folder(self) -> None:
        start_dir = self.bulk_folder_input.text().strip() or str(default_download_dir())
        chosen = QFileDialog.getExistingDirectory(self, "Choose bulk download folder", start_dir)
        if chosen:
            self.bulk_folder_input.setText(chosen)
            self._save_settings()

    def _set_busy(self, busy: bool) -> None:
        self._download_fields.setEnabled(not busy)
        self.download_button.setEnabled(not busy)
        self.playlist_subfolder_checkbox.setEnabled(not busy)
        self.attach_metadata_checkbox.setEnabled(not busy)
        if not busy:
            self.progress.setValue(0)
            self._on_url_changed()

    def _warn(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)

    def _prepare_download(self) -> tuple[str, Path, ClipRange | None] | None:
        url = self.url_input.text().strip()
        folder = self.folder_input.text().strip()
        clip_start = self.clip_start_input.text().strip() or None
        clip_end = self.clip_end_input.text().strip() or None

        if not url:
            self._warn("Missing URL", "Enter a Suno song or playlist URL, or an ID.")
            return None
        if not folder:
            self._warn("Missing folder", "Choose a download folder.")
            return None

        try:
            kind, _ = parse_suno_input(url)
        except ValueError as exc:
            self._warn("Invalid input", str(exc))
            return None

        if kind == "playlist" and (clip_start or clip_end):
            self._warn(
                "Clip not supported",
                "Clip start/end applies to single songs only, not playlists or hooks.",
            )
            return None

        if kind == "hook" and (clip_start or clip_end):
            self._warn(
                "Clip not supported",
                "Clip start/end applies to single songs only, not hooks.",
            )
            return None

        try:
            clip = clip_from_optional_strings(clip_start, clip_end)
        except ValueError as exc:
            self._warn("Invalid clip time", str(exc))
            return None

        return url, Path(folder), clip

    def _start_download(self) -> None:
        prepared = self._prepare_download()
        if prepared is None:
            return
        url_input, output_dir, clip = prepared

        self._save_settings()
        self._set_busy(True)
        self.progress.setValue(0)
        self.status_label.setText("Fetching info…")

        self.worker = DownloadWorker(
            url_input,
            output_dir,
            use_subfolder=self.playlist_subfolder_checkbox.isChecked(),
            clip=clip,
            attach_metadata=self.attach_metadata_checkbox.isChecked(),
            hook_format=self._hook_format(),
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.status.connect(self.status_label.setText)
        self.worker.finished_ok.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            pct = min(100, done * 100 // total)
            self.progress.setValue(pct)
            if not self.status_label.text().startswith("Downloading "):
                self.status_label.setText(f"Downloading… {pct}%")
            return

        self.progress.setValue(0)
        if done > 0:
            self.status_label.setText(f"Downloading… {done:,} bytes")
        else:
            self.status_label.setText("Downloading…")

    def _on_finished(self, result: object) -> None:
        assert isinstance(result, DownloadResult)
        self._set_busy(False)
        self.progress.setValue(100)

        bitrate = None
        if result.song and result.song.bitrate_kbps:
            bitrate = result.song.bitrate_kbps
        elif result.playlist:
            bitrate = next(
                (clip.bitrate_kbps for clip in result.playlist.clips if clip.bitrate_kbps),
                None,
            )
        if bitrate:
            self.detected_bitrate = bitrate
            self._refresh_format_options()

        if result.playlist is not None:
            fail_note = f", {len(result.failures)} failed" if result.failures else ""
            self.status_label.setText(
                f"Playlist complete — {len(result.paths)} songs saved to "
                f"{result.saved_to}{fail_note}"
            )
            return

        if result.hook is not None:
            self.status_label.setText(
                f"Hook complete — {len(result.paths)} file(s) saved to {result.saved_to}"
            )
            return

        self.status_label.setText(f"Download complete — saved to {result.saved_to}")

    def _on_failed(self, message: str) -> None:
        self._set_busy(False)
        self.progress.setValue(0)
        self.status_label.setText("Download failed.")
        QMessageBox.critical(self, "Download failed", message)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_settings()
        for worker in (self.worker, self.profile_worker, self.bulk_worker):
            if worker and worker.isRunning():
                worker.wait(3000)
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Suno MP3 Downloader")
    app.setOrganizationName(SETTINGS_ORG)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


# key, light, dark — compared side by side so the two themes stay in sync
_THEME_PAIRS = """
window_bg              #e8ecf1  #0a0a0a
text                   #2d3748  #e8e8e8
surface                #f2f5f8  #141414
surface_border         #c5cdd8  #2e2e2e
title                  #374151  #e8e8e8
input_bg               #f8fafc  #1c1c1c
input_border           #b8c2ce  #333333
input_focus_border     #5b8fd9  #4a9eff
input_focus_bg         #fbfcfd  #1c1c1c
combo_selection        #dce6f2  #2a2a2a
check_border           #b8c2ce  #444444
check_bg               #f8fafc  #1c1c1c
accent                 #5b8fd9  #4a9eff
button_bg              #5b8fd9  #3b82f6
button_text            #f8fafc  #ffffff
button_hover           #4a7ec8  #2563eb
button_disabled_bg     #a8b4c0  #2a2a2a
button_disabled_text   #e8ecf1  #666666
browse_bg              #d5dde6  #2a2a2a
browse_hover           #c5cdd8  #333333
progress_bg            #d5dde6  #1c1c1c
progress_text          #374151  #e8e8e8
progress_chunk         #5fa87a  #22c55e
hint                   #5c6b7a  #999999
status                 #4a5568  #b0b0b0
tab_bg                 #d5dde6  #1a1a1a
tab_text               #4a5568  #999999
tab_hover_bg           #e2e8f0  #222222
tab_hover_text         #4a5568  #cccccc
"""


def _parse_themes(table: str) -> tuple[dict[str, str], dict[str, str]]:
    light: dict[str, str] = {}
    dark: dict[str, str] = {}
    for line in table.strip().splitlines():
        key, light_val, dark_val = line.split()
        light[key] = light_val
        dark[key] = dark_val
    return light, dark


LIGHT_THEME, DARK_THEME = _parse_themes(_THEME_PAIRS)

_STYLE = Template("""
QMainWindow, QWidget#centralWidget, QTabWidget {
    background-color: $window_bg;
    color: $text;
}
QWidget {
    color: $text;
    font-family: "Segoe UI", "SF Pro Text", sans-serif;
    font-size: 13px;
    background: transparent;
}
QLabel {
    background: transparent;
    border: none;
}
QFrame#sectionFrame {
    border: 1px solid $surface_border;
    border-radius: 10px;
    background: $surface;
}
QLabel#sectionTitle {
    color: $title;
    background-color: $surface;
    font-weight: 600;
    padding: 0 6px;
    border: none;
}
QLineEdit, QComboBox, QDateEdit {
    background: $input_bg;
    border: 1px solid $input_border;
    border-radius: 8px;
    padding: 8px 12px;
    min-height: 22px;
    color: $text;
}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus {
    border: 1px solid $input_focus_border;
    background: $input_focus_bg;
}
QLineEdit#bulkFilterControl, QComboBox#bulkFilterControl, QDateEdit#bulkFilterControl {
    padding: 4px 8px;
    min-height: 18px;
}
QPushButton#bulkFilterButton {
    background-color: $browse_bg;
    color: $text;
    padding: 4px 8px;
    min-height: 18px;
    font-weight: 500;
    font-size: 12px;
}
QPushButton#bulkFilterButton:hover {
    background-color: $browse_hover;
}
QComboBox QAbstractItemView {
    background: $input_bg;
    color: $text;
    border: 1px solid $input_border;
    selection-background-color: $combo_selection;
}
QCheckBox {
    spacing: 8px;
    background: transparent;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid $check_border;
    background: $check_bg;
}
QCheckBox::indicator:checked {
    background-color: $accent;
    border-color: $accent;
    image: url($check_image);
}
QPushButton {
    background-color: $button_bg;
    color: $button_text;
    border: none;
    border-radius: 8px;
    padding: 8px 18px;
    min-height: 22px;
    font-weight: 600;
}
QPushButton:hover {
    background-color: $button_hover;
}
QPushButton:disabled {
    background-color: $button_disabled_bg;
    color: $button_disabled_text;
}
QPushButton#smallButton {
    background-color: $browse_bg;
    color: $text;
    padding: 4px 8px;
    min-height: 18px;
    font-weight: 500;
    font-size: 12px;
}
QPushButton#smallButton:hover {
    background-color: $browse_hover;
}
QPushButton#browseButton {
    background-color: $browse_bg;
    color: $text;
}
QPushButton#browseButton:hover {
    background-color: $browse_hover;
}
QProgressBar {
    border: none;
    border-radius: 6px;
    background: $progress_bg;
    text-align: center;
    color: $progress_text;
    min-height: 18px;
}
QProgressBar::chunk {
    border-radius: 6px;
    background: $progress_chunk;
}
QLabel#hintLabel {
    color: $hint;
    font-size: 12px;
}
QLabel#statusLabel {
    color: $status;
}
QTabWidget::pane {
    border: 1px solid $surface_border;
    border-radius: 8px;
    background: $surface;
    margin-top: 0;
}
QTabBar {
    background: transparent;
    border: none;
}
QTabBar::tab {
    background: $tab_bg;
    color: $tab_text;
    border: 1px solid $surface_border;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 6px 14px;
    margin-right: 4px;
    margin-top: 2px;
    min-height: 18px;
}
QTabBar::tab:selected {
    background: $surface;
    color: $text;
    font-weight: 600;
    border-color: $surface_border;
    margin-bottom: -1px;
    padding-bottom: 7px;
}
QTabBar::tab:!selected {
    margin-top: 4px;
    padding-bottom: 5px;
}
QTabBar::tab:hover:!selected {
    background: $tab_hover_bg;
    color: $tab_hover_text;
}
QTableWidget {
    background: $input_bg;
    border: 1px solid $input_border;
    border-radius: 8px;
    gridline-color: $surface_border;
}
QTableWidget::item {
    padding: 4px;
}
QHeaderView::section {
    background: $browse_bg;
    color: $text;
    border: none;
    border-bottom: 1px solid $surface_border;
    padding: 6px;
}
QTextBrowser {
    background: $input_bg;
    border: 1px solid $input_border;
    border-radius: 8px;
    padding: 8px;
}
QTabWidget > QWidget {
    background: $surface;
}
""")


def build_stylesheet(dark: bool) -> str:
    check = (Path(__file__).resolve().parent / "assets" / "checkbox_check.svg").as_posix()
    theme = DARK_THEME if dark else LIGHT_THEME
    return _STYLE.substitute(check_image=check, **theme)


if __name__ == "__main__":
    main()
