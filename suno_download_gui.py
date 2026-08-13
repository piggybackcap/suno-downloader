"""PySide6 GUI for downloading Suno songs as MP3."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

from PySide6.QtCore import QSize, Qt, QSettings, QThread, Signal
from PySide6.QtGui import QCloseEvent, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from download_song_mp3 import (
    DEFAULT_MP3_BITRATE_KBPS,
    ClipRange,
    PlaylistMetadata,
    SongMetadata,
    clip_from_optional_strings,
    download_playlist_mp3,
    download_song_mp3,
    parse_suno_input,
)

SETTINGS_ORG = "suno-scraper"
SETTINGS_APP = "download-gui"

# Custom stylesheet + group boxes don't get usable Qt defaults; these match the current look.
MARGIN_WINDOW = 18
MARGIN_TAB = 14
PADDING_SECTION = 14
SPACING_SECTION = 14
SPACING_INNER = 6
SPACING_ROW = 12


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
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

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


def _add_group(parent: QVBoxLayout, title: str) -> QVBoxLayout:
    section = Section(title)
    parent.addWidget(section)
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


@dataclass
class DownloadResult:
    saved_to: str
    song: SongMetadata | None = None
    playlist: PlaylistMetadata | None = None
    paths: list[Path] = field(default_factory=list)
    failures: list[tuple[SongMetadata, str]] = field(default_factory=list)


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
    ) -> None:
        super().__init__()
        self.url_input = url_input
        self.output_dir = output_dir
        self.use_subfolder = use_subfolder
        self.clip = clip

    def run(self) -> None:
        try:
            kind, resource_id = parse_suno_input(self.url_input)

            def on_progress(done: int, total: int | None) -> None:
                self.progress.emit(done, total or 0)

            if kind == "song":
                path, metadata = download_song_mp3(
                    resource_id,
                    self.output_dir,
                    progress=on_progress,
                    clip=self.clip,
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
        self.detected_bitrate = DEFAULT_MP3_BITRATE_KBPS

        self.setWindowTitle("Suno MP3 Downloader")
        self.setMinimumSize(640, 560)
        # Apply the stylesheet before building widgets so layout sizeHints include
        # styled padding/min-height (otherwise labels layout against unstyled heights).
        self._apply_theme(self.settings.value("dark_mode", False, type=bool))
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
            "https://suno.com/song/... or https://suno.com/playlist/..."
        )
        self.url_input.textChanged.connect(self._on_url_changed)
        source = _add_group(fields_layout, "Source")
        source.addWidget(self.url_input)
        source.addWidget(
            _hint(
                "Paste a Suno song or playlist URL, or a 36-character ID.\n"
                "Song: https://suno.com/song/094ac41f-93f5-4f12-a93b-2c74940b69b7\n"
                "Playlist: https://suno.com/playlist/7a8259af-549e-47f3-874e-6d0f1d76e272"
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

        appearance = _add_group(layout, "Appearance")
        self.dark_mode_checkbox = QCheckBox("Dark mode")
        self.dark_mode_checkbox.toggled.connect(self._on_dark_mode_toggled)
        appearance.addWidget(self.dark_mode_checkbox)

        layout.addStretch()
        return tab

    def _set_checkbox(self, checkbox: QCheckBox, key: str, default: bool) -> None:
        checkbox.blockSignals(True)
        checkbox.setChecked(self.settings.value(key, default, type=bool))
        checkbox.blockSignals(False)

    def _load_settings(self) -> None:
        folder = self.settings.value("output_dir", str(default_download_dir()))
        self.folder_input.setText(str(folder))
        self._set_checkbox(self.dark_mode_checkbox, "dark_mode", False)
        self._set_checkbox(self.playlist_subfolder_checkbox, "playlist_subfolder", True)

    def _save_settings(self) -> None:
        self.settings.setValue("output_dir", self.folder_input.text().strip())
        self.settings.setValue("dark_mode", self.dark_mode_checkbox.isChecked())
        self.settings.setValue("playlist_subfolder", self.playlist_subfolder_checkbox.isChecked())

    def _refresh_format_options(self) -> None:
        self.format_combo.clear()
        self.format_combo.addItem(f"MP3 ({self.detected_bitrate} kbps)", "mp3")
        self.format_combo.setEnabled(False)

    def _apply_theme(self, dark: bool) -> None:
        self.setStyleSheet(build_stylesheet(dark))

    def _on_dark_mode_toggled(self, enabled: bool) -> None:
        self._apply_theme(enabled)
        self._save_settings()

    def _on_url_changed(self, _text: str = "") -> None:
        is_playlist = False
        text = self.url_input.text().strip()
        if text:
            try:
                kind, _ = parse_suno_input(text)
                is_playlist = kind == "playlist"
            except ValueError:
                pass

        self.clip_start_input.setEnabled(not is_playlist)
        self.clip_end_input.setEnabled(not is_playlist)
        if is_playlist:
            self.clip_start_input.clear()
            self.clip_end_input.clear()

    def _choose_folder(self) -> None:
        start_dir = self.folder_input.text().strip() or str(default_download_dir())
        chosen = QFileDialog.getExistingDirectory(self, "Choose download folder", start_dir)
        if chosen:
            self.folder_input.setText(chosen)
            self._save_settings()

    def _set_busy(self, busy: bool) -> None:
        self._download_fields.setEnabled(not busy)
        self.download_button.setEnabled(not busy)
        self.playlist_subfolder_checkbox.setEnabled(not busy)
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
                "Clip start/end applies to single songs only, not playlists.",
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

        self.status_label.setText(f"Download complete — saved to {result.saved_to}")

    def _on_failed(self, message: str) -> None:
        self._set_busy(False)
        self.progress.setValue(0)
        self.status_label.setText("Download failed.")
        QMessageBox.critical(self, "Download failed", message)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_settings()
        if self.worker and self.worker.isRunning():
            self.worker.wait(3000)
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
QLineEdit, QComboBox {
    background: $input_bg;
    border: 1px solid $input_border;
    border-radius: 8px;
    padding: 8px 12px;
    min-height: 22px;
    color: $text;
}
QLineEdit:focus, QComboBox:focus {
    border: 1px solid $input_focus_border;
    background: $input_focus_bg;
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
