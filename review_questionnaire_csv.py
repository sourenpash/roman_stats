from __future__ import annotations

import argparse
import csv
import hashlib
from pathlib import Path
import subprocess
import tempfile
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import tkinter as tk
    from tkinter import messagebox, ttk
    from PIL import Image, ImageOps, ImageTk
else:  # These are imported lazily in main() so --help still works without Tk installed.
    tk = Any
    ttk = Any
    messagebox = Any
    Image = Any
    ImageOps = Any
    ImageTk = Any

try:
    from pillow_heif import register_heif_opener
except ImportError:  # pragma: no cover - optional dependency
    register_heif_opener = None


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CSV_PATH = SCRIPT_DIR / "csvs" / "questionnaire_results.csv"
DEFAULT_IMAGE_DIR = SCRIPT_DIR / "images"
QUESTION_COLUMNS = [f"Q{i}" for i in range(3, 22)]
TEXT_COLUMNS = ["age_group", "gender", "comment", "application_area"]
EDITABLE_COLUMNS = TEXT_COLUMNS + QUESTION_COLUMNS
IMAGE_PREVIEW_SIZE = (520, 760)
RESAMPLE_LANCZOS = None


def register_heif_support() -> bool:
    if register_heif_opener is None:
        return False
    register_heif_opener()
    return True


class ReviewApp:
    def __init__(self, root: tk.Tk, csv_path: Path, image_dir: Path) -> None:
        self.root = root
        self.csv_path = csv_path
        self.image_dir = image_dir
        self.heif_enabled = register_heif_support()
        self.fieldnames, self.rows = self._load_csv()
        self.current_index = 0
        self.is_loading_row = False
        self.photo_refs: list[ImageTk.PhotoImage | None] = [None, None]
        self.text_widgets: dict[str, tk.Text] = {}
        self.preview_temp_dir = tempfile.TemporaryDirectory(prefix="questionnaire_review_")

        self.root.title("Questionnaire CSV Review")
        self.root.geometry("1600x950")
        self.root.minsize(1280, 800)

        self.status_var = tk.StringVar()
        self.path_var = tk.StringVar()
        self.image_status_var = tk.StringVar()
        self.row_var = tk.StringVar(value="1")
        self.dirty_var = tk.StringVar(value="Saved")

        self.field_vars = {column: tk.StringVar() for column in TEXT_COLUMNS + QUESTION_COLUMNS}
        for variable in self.field_vars.values():
            variable.trace_add("write", self._mark_dirty)

        self._build_ui()
        self._bind_shortcuts()
        self.load_row(0)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _load_csv(self) -> tuple[list[str], list[dict[str, str]]]:
        if not self.csv_path.exists():
            raise FileNotFoundError(f"CSV file not found: {self.csv_path}")

        with self.csv_path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            rows = [{key: value or "" for key, value in row.items()} for row in reader]

        if not rows:
            raise ValueError(f"CSV file has no reviewable rows: {self.csv_path}")

        for column in EDITABLE_COLUMNS + ["image_files"]:
            if column not in fieldnames:
                fieldnames.append(column)
        normalized_rows: list[dict[str, str]] = []
        for row in rows:
            normalized_rows.append({column: row.get(column, "") for column in fieldnames})
        return fieldnames, normalized_rows

    def _write_rows(self) -> None:
        temp_path = self.csv_path.with_suffix(f"{self.csv_path.suffix}.tmp")
        with temp_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.fieldnames)
            writer.writeheader()
            writer.writerows(self.rows)
        temp_path.replace(self.csv_path)

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=12)
        container.pack(fill="both", expand=True)

        top_bar = ttk.Frame(container)
        top_bar.pack(fill="x", pady=(0, 10))

        ttk.Button(top_bar, text="Previous", command=lambda: self.change_row(-1)).pack(side="left")
        ttk.Button(top_bar, text="Next", command=lambda: self.change_row(1)).pack(side="left", padx=(8, 16))
        ttk.Button(top_bar, text="Save Row", command=self.save_current_row).pack(side="left")

        ttk.Label(top_bar, text="Go to row:").pack(side="left", padx=(20, 4))
        row_spinbox = ttk.Spinbox(
            top_bar,
            from_=1,
            to=max(len(self.rows), 1),
            width=6,
            textvariable=self.row_var,
            command=self.go_to_row_from_field,
        )
        row_spinbox.pack(side="left")
        ttk.Button(top_bar, text="Go", command=self.go_to_row_from_field).pack(side="left", padx=(6, 16))

        ttk.Label(top_bar, textvariable=self.status_var).pack(side="left")
        tk.Label(top_bar, textvariable=self.dirty_var, fg="#8b0000").pack(side="right")

        ttk.Label(container, textvariable=self.path_var).pack(fill="x", pady=(0, 6))
        tk.Label(container, textvariable=self.image_status_var, fg="#8b0000", anchor="w").pack(
            fill="x", pady=(0, 10)
        )

        content = ttk.Panedwindow(container, orient="horizontal")
        content.pack(fill="both", expand=True)

        image_frame = ttk.Frame(content, padding=(0, 0, 12, 0))
        form_frame = ttk.Frame(content)
        content.add(image_frame, weight=2)
        content.add(form_frame, weight=3)

        images_container = ttk.Frame(image_frame)
        images_container.pack(fill="both", expand=True)
        images_container.columnconfigure(0, weight=1)
        images_container.columnconfigure(1, weight=1)
        images_container.rowconfigure(0, weight=1)

        self.image_name_vars = [tk.StringVar(), tk.StringVar()]
        self.image_labels: list[ttk.Label] = []
        for idx in range(2):
            pane = ttk.LabelFrame(images_container, text=f"Image {idx + 1}", padding=8)
            pane.grid(row=0, column=idx, sticky="nsew", padx=(0, 6) if idx == 0 else (6, 0))
            ttk.Label(pane, textvariable=self.image_name_vars[idx]).pack(anchor="w", pady=(0, 6))
            image_label = ttk.Label(pane, anchor="center", justify="center")
            image_label.pack(fill="both", expand=True)
            self.image_labels.append(image_label)

        meta_frame = ttk.LabelFrame(form_frame, text="Metadata", padding=10)
        meta_frame.pack(fill="x", pady=(0, 10))
        self._add_labeled_entry(meta_frame, "age_group", "Age group", 0, 0)
        self._add_labeled_entry(meta_frame, "gender", "Gender", 0, 2)

        questions_frame = ttk.LabelFrame(form_frame, text="Likert Responses", padding=10)
        questions_frame.pack(fill="x", pady=(0, 10))
        for idx, column in enumerate(QUESTION_COLUMNS):
            row = idx // 5
            col = (idx % 5) * 2
            ttk.Label(questions_frame, text=column).grid(row=row, column=col, sticky="w", padx=(0, 6), pady=4)
            entry = ttk.Entry(questions_frame, textvariable=self.field_vars[column], width=5)
            entry.grid(row=row, column=col + 1, sticky="w", padx=(0, 18), pady=4)

        comments_frame = ttk.LabelFrame(form_frame, text="Open Text", padding=10)
        comments_frame.pack(fill="both", expand=True)
        self._add_text_widget(comments_frame, "comment", "Comment", 0)
        self._add_text_widget(comments_frame, "application_area", "Application area", 1)

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Control-s>", lambda _: self.save_current_row())
        self.root.bind("<Command-s>", lambda _: self.save_current_row())
        self.root.bind("<Left>", lambda _: self.change_row(-1))
        self.root.bind("<Right>", lambda _: self.change_row(1))

    def _add_labeled_entry(
        self,
        frame: ttk.LabelFrame,
        column_name: str,
        label: str,
        row: int,
        column: int,
    ) -> None:
        ttk.Label(frame, text=label).grid(row=row, column=column, sticky="w", padx=(0, 6), pady=4)
        entry = ttk.Entry(frame, textvariable=self.field_vars[column_name], width=32)
        entry.grid(row=row, column=column + 1, sticky="ew", padx=(0, 18), pady=4)
        frame.columnconfigure(column + 1, weight=1)

    def _add_text_widget(self, frame: ttk.LabelFrame, column_name: str, label: str, row: int) -> None:
        ttk.Label(frame, text=label).grid(row=row * 2, column=0, sticky="w", pady=(0, 4))
        text_widget = tk.Text(frame, wrap="word", height=6)
        text_widget.grid(row=row * 2 + 1, column=0, sticky="nsew", pady=(0, 12))
        text_widget.bind("<<Modified>>", lambda event, name=column_name: self._on_text_modified(event, name))
        self.text_widgets[column_name] = text_widget
        frame.rowconfigure(row * 2 + 1, weight=1)
        frame.columnconfigure(0, weight=1)

    def _on_text_modified(self, event: tk.Event, column_name: str) -> None:
        widget = event.widget
        if widget.edit_modified():
            if not self.is_loading_row:
                self._mark_dirty()
            widget.edit_modified(False)

    def _mark_dirty(self, *_args: object) -> None:
        if self.is_loading_row:
            return
        self.dirty_var.set("Unsaved changes")

    def _clear_dirty(self) -> None:
        self.dirty_var.set("Saved")

    def load_row(self, index: int) -> None:
        index = max(0, min(index, len(self.rows) - 1))
        self.current_index = index
        row = self.rows[index]

        self.is_loading_row = True
        try:
            for column in self.field_vars:
                self.field_vars[column].set(str(row.get(column, "")))

            for column_name, widget in self.text_widgets.items():
                widget.delete("1.0", "end")
                widget.insert("1.0", str(row.get(column_name, "")))
                widget.edit_modified(False)
        finally:
            self.is_loading_row = False

        self.row_var.set(str(index + 1))
        self.status_var.set(f"Row {index + 1} of {len(self.rows)}")
        self.path_var.set(f"CSV: {self.csv_path}    Images: {self.image_dir}")
        self._clear_dirty()
        self._load_images()

    def collect_form_values(self) -> dict[str, str]:
        values = {column: self.field_vars[column].get().strip() for column in self.field_vars}
        for column_name, widget in self.text_widgets.items():
            values[column_name] = widget.get("1.0", "end-1c").strip()
        return values

    def validate_form_values(self, values: dict[str, str]) -> tuple[bool, str]:
        for column in QUESTION_COLUMNS:
            raw_value = values[column].strip()
            if raw_value == "":
                values[column] = ""
                continue
            if not raw_value.isdigit():
                return False, f"{column} must be an integer from 1 to 5 or left blank."
            number = int(raw_value)
            if number < 1 or number > 5:
                return False, f"{column} must be an integer from 1 to 5 or left blank."
            values[column] = str(number)
        return True, ""

    def save_current_row(self) -> bool:
        values = self.collect_form_values()
        is_valid, error_message = self.validate_form_values(values)
        if not is_valid:
            messagebox.showerror("Invalid value", error_message)
            return False

        for column, value in values.items():
            self.rows[self.current_index][column] = value

        self._write_rows()
        self._clear_dirty()
        self.status_var.set(f"Row {self.current_index + 1} of {len(self.rows)} saved")
        return True

    def prompt_to_save_if_needed(self) -> bool:
        if self.dirty_var.get() != "Unsaved changes":
            return True

        answer = messagebox.askyesnocancel(
            "Unsaved changes",
            "Save the current row before leaving it?",
        )
        if answer is None:
            return False
        if answer:
            return self.save_current_row()
        return True

    def change_row(self, delta: int) -> None:
        if not self.prompt_to_save_if_needed():
            return
        target_index = self.current_index + delta
        if target_index < 0 or target_index >= len(self.rows):
            return
        self.load_row(target_index)

    def go_to_row_from_field(self) -> None:
        if not self.prompt_to_save_if_needed():
            return
        try:
            index = int(self.row_var.get()) - 1
        except ValueError:
            messagebox.showerror("Invalid row", "Enter a valid row number.")
            self.row_var.set(str(self.current_index + 1))
            return
        if index < 0 or index >= len(self.rows):
            messagebox.showerror("Invalid row", f"Enter a row from 1 to {len(self.rows)}.")
            self.row_var.set(str(self.current_index + 1))
            return
        self.load_row(index)

    def _load_images(self) -> None:
        image_files_raw = str(self.rows[self.current_index].get("image_files", ""))
        filenames = [part.strip() for part in image_files_raw.split(",") if part.strip()]
        status_messages: list[str] = []

        for idx in range(2):
            label = self.image_labels[idx]
            filename_var = self.image_name_vars[idx]
            self.photo_refs[idx] = None

            if idx >= len(filenames):
                filename_var.set("No image listed")
                label.configure(image="", text="No image listed for this row.")
                continue

            filename = filenames[idx]
            filename_var.set(filename)
            image_path = self.resolve_image_path(filename)
            if image_path is None:
                label.configure(image="", text=f"Image not found:\n{filename}")
                status_messages.append(f"Missing file: {filename}")
                continue

            try:
                image = self.load_preview_image(image_path)
                image.thumbnail(IMAGE_PREVIEW_SIZE, RESAMPLE_LANCZOS)
                photo = ImageTk.PhotoImage(image)
            except Exception as exc:  # pragma: no cover - Tk/image backend issues vary by machine
                label.configure(image="", text=f"Could not load:\n{image_path.name}\n\n{exc}")
                if image_path.suffix.lower() == ".heic" and not self.heif_enabled:
                    status_messages.append(
                        f"Could not decode {image_path.name}. Install pillow-heif or use the macOS sips fallback."
                    )
                else:
                    status_messages.append(f"Could not load {image_path.name}: {exc}")
                continue

            self.photo_refs[idx] = photo
            label.configure(image=photo, text="")

        self.image_status_var.set(" | ".join(status_messages))

    def load_preview_image(self, image_path: Path) -> Image.Image:
        try:
            return self._open_image(image_path)
        except Exception:
            if image_path.suffix.lower() != ".heic":
                raise
            fallback_path = self.convert_heic_with_sips(image_path)
            return self._open_image(fallback_path)

    def _open_image(self, image_path: Path) -> Image.Image:
        with Image.open(image_path) as opened_image:
            image = ImageOps.exif_transpose(opened_image)
            return image.copy()

    def convert_heic_with_sips(self, image_path: Path) -> Path:
        preview_name = f"{image_path.stem}_{hashlib.sha1(str(image_path).encode('utf-8')).hexdigest()[:10]}.png"
        preview_path = Path(self.preview_temp_dir.name) / preview_name
        if preview_path.exists():
            return preview_path

        completed = subprocess.run(
            ["sips", "-s", "format", "png", str(image_path), "--out", str(preview_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0 or not preview_path.exists():
            details = completed.stderr.strip() or completed.stdout.strip() or "Unknown conversion error."
            raise RuntimeError(f"macOS sips conversion failed for {image_path.name}: {details}")
        return preview_path

    def resolve_image_path(self, filename: str) -> Path | None:
        candidate = Path(filename)
        if candidate.is_absolute() and candidate.exists():
            return candidate

        search_paths = [
            self.image_dir / filename,
            self.csv_path.parent / filename,
            SCRIPT_DIR / filename,
        ]
        for path in search_paths:
            if path.exists():
                return path
        return None

    def on_close(self) -> None:
        if not self.prompt_to_save_if_needed():
            return
        self.preview_temp_dir.cleanup()
        self.root.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Review questionnaire CSV rows against their source images in a local Tkinter GUI."
    )
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV_PATH, help="Path to questionnaire_results.csv")
    parser.add_argument("--images", type=Path, default=DEFAULT_IMAGE_DIR, help="Directory containing source images")
    return parser.parse_args()


def main() -> None:
    global tk, ttk, messagebox, Image, ImageOps, ImageTk, RESAMPLE_LANCZOS, register_heif_opener
    args = parse_args()
    try:
        import tkinter as tk  # type: ignore[no-redef]
        from tkinter import messagebox, ttk  # type: ignore[no-redef]
        from PIL import Image, ImageOps, ImageTk  # type: ignore[no-redef]
    except ModuleNotFoundError as exc:
        missing_name = getattr(exc, "name", "")
        if missing_name == "_tkinter":
            raise SystemExit(
                "Tkinter is not available in this Python installation. "
                "Install a Python build with Tk support, then rerun this script."
            ) from exc
        if missing_name == "PIL":
            raise SystemExit(
                "Pillow is not installed. Install Pillow, and optionally pillow-heif for HEIC files, "
                "then rerun this script."
            ) from exc
        raise

    try:
        from pillow_heif import register_heif_opener  # type: ignore[no-redef]
    except ImportError:
        register_heif_opener = None

    RESAMPLE_LANCZOS = getattr(Image, "Resampling", Image).LANCZOS

    root = tk.Tk()
    try:
        ReviewApp(root, csv_path=args.csv.expanduser().resolve(), image_dir=args.images.expanduser().resolve())
    except Exception as exc:
        root.withdraw()
        messagebox.showerror("Could not open review GUI", str(exc))
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
