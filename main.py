import os
import re
import sys
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import fitz  # pymupdf
import pyttsx3


BOOKS_DIR = "/Volumes/macStore/pythonProjects/PythonProject/PdfNarrator/Books"

CHAPTER_PATTERNS = [
    re.compile(r"^\s*(chapter)\s+([0-9]+|[ivxlcdm]+)\b\s*(.*)$", re.IGNORECASE),   # CHAPTER 1 / CHAPTER II
    re.compile(r"^\s*(chapter)\s+([a-z]+)\b\s*(.*)$", re.IGNORECASE),             # CHAPTER ONE
    re.compile(r"^\s*(ch\.?|chap\.?)\s*[-:]?\s*([0-9]+|[ivxlcdm]+)\b\s*(.*)$", re.IGNORECASE),  # CH. 1 / CHAP. 3
]

# If the TOC exists, we will treat these TOC entries as chapters when matched
TOC_CHAPTER_HINT = re.compile(r"\bchapter\b|\bch\.\b|\bchap\.\b", re.IGNORECASE)


@dataclass
class Chapter:
    label: str            # e.g., "1", "II", "One", or "TOC"
    title: str            # chapter title (if known)
    start_page: int       # 0-based
    end_page: int         # 0-based inclusive


def normalize_whitespace(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def list_pdf_files(folder: str) -> List[str]:
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"Folder not found: {folder}")

    pdfs = []
    for name in os.listdir(folder):
        if name.lower().endswith(".pdf"):
            pdfs.append(os.path.join(folder, name))
    return sorted(pdfs, key=lambda p: os.path.basename(p).lower())


def choose_from_list(items: List[str], prompt: str) -> int:
    while True:
        choice = input(prompt).strip()
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(items):
                return idx
        print("Invalid selection. Please enter a number from the list.")


def init_tts() -> pyttsx3.Engine:
    return pyttsx3.init()


def list_voices(engine: pyttsx3.Engine) -> None:
    voices = engine.getProperty("voices")
    for i, v in enumerate(voices):
        name = getattr(v, "name", "Unknown")
        vid = getattr(v, "id", "")
        langs = getattr(v, "languages", [])
        print(f"[{i}] {name} | id={vid} | languages={langs}")


def set_voice(engine: pyttsx3.Engine, voice_index: int) -> None:
    voices = engine.getProperty("voices")
    if 0 <= voice_index < len(voices):
        engine.setProperty("voice", voices[voice_index].id)
        print(f"Voice set to: {voices[voice_index].name}")
    else:
        raise ValueError("Invalid voice index")


def set_rate(engine: pyttsx3.Engine, rate: int) -> None:
    engine.setProperty("rate", rate)
    print(f"Speech rate set to: {rate}")


def page_text(doc: fitz.Document, page_index: int) -> str:
    return doc[page_index].get_text("text") or ""


def find_chapter_heading_on_page(text: str) -> Optional[Tuple[str, str]]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None

    scan_lines = lines[:25]  # chapter headings usually appear near the top
    for ln in scan_lines:
        if len(ln) > 140:
            continue
        for pat in CHAPTER_PATTERNS:
            m = pat.match(ln)
            if m:
                label = normalize_whitespace(m.group(2))
                title = normalize_whitespace(m.group(3) if m.lastindex and m.lastindex >= 3 else "")
                title = re.sub(r"^[-:–—]+\s*", "", title).strip()
                return label, title
    return None


def chapters_from_toc(doc: fitz.Document) -> List[Chapter]:
    """
    Prefer the PDF Table of Contents (outline) if present.
    PyMuPDF: doc.get_toc() returns list of [level, title, page] (page is 1-based).
    """
    toc = doc.get_toc()
    if not toc:
        return []

    # Candidate TOC entries. Heuristic:
    # - Prefer entries that look like chapters (title contains "chapter" / "ch.")
    # - If none match, fall back to level==1 entries.
    chapter_like = [(lvl, title, page) for (lvl, title, page) in toc if TOC_CHAPTER_HINT.search(title or "")]
    if chapter_like:
        entries = chapter_like
    else:
        entries = [(lvl, title, page) for (lvl, title, page) in toc if lvl == 1]

    # Convert to start pages (0-based), merge duplicates, sort by page
    starts = []
    seen_pages = set()
    for lvl, title, page1 in entries:
        if not page1:
            continue
        start0 = max(0, min(int(page1) - 1, len(doc) - 1))
        if start0 in seen_pages:
            continue
        seen_pages.add(start0)
        starts.append((start0, normalize_whitespace(title or "")))

    starts.sort(key=lambda x: x[0])
    if not starts:
        return []

    chapters: List[Chapter] = []
    for i, (sp, title) in enumerate(starts):
        ep = (starts[i + 1][0] - 1) if (i + 1 < len(starts)) else (len(doc) - 1)
        label = "TOC"
        chapters.append(Chapter(label=label, title=title, start_page=sp, end_page=max(sp, ep)))

    # Remove tiny / bogus ranges (e.g., consecutive TOC entries on same page)
    filtered = []
    last_sp = -9999
    for ch in chapters:
        if ch.start_page - last_sp >= 1:
            filtered.append(ch)
            last_sp = ch.start_page
    return filtered


def chapters_by_scanning(doc: fitz.Document) -> List[Chapter]:
    hits = []
    for i in range(len(doc)):
        txt = page_text(doc, i)
        hit = find_chapter_heading_on_page(txt)
        if hit:
            label, title = hit
            hits.append((i, label, title))

    if not hits:
        return []

    # Deduplicate: ignore repeated headings in running headers
    dedup = []
    last_page = -9999
    for page_i, label, title in hits:
        if page_i - last_page >= 2:
            dedup.append((page_i, label, title))
            last_page = page_i

    chapters: List[Chapter] = []
    for idx, (sp, label, title) in enumerate(dedup):
        ep = (dedup[idx + 1][0] - 1) if (idx + 1 < len(dedup)) else (len(doc) - 1)
        chapters.append(Chapter(label=label, title=title, start_page=sp, end_page=max(sp, ep)))

    return chapters


def detect_chapters(doc: fitz.Document) -> List[Chapter]:
    # 1) Try TOC
    toc_ch = chapters_from_toc(doc)
    if toc_ch:
        return toc_ch

    # 2) Fallback to scanning pages
    scan_ch = chapters_by_scanning(doc)
    return scan_ch


def chunk_text_for_speaking(text: str, max_chars: int = 900) -> List[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[str] = []
    for p in paragraphs:
        p = normalize_whitespace(p)
        if len(p) <= max_chars:
            chunks.append(p)
        else:
            parts = re.split(r"(?<=[.!?])\s+", p)
            buf = ""
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                if len(buf) + len(part) + 1 <= max_chars:
                    buf = (buf + " " + part).strip()
                else:
                    if buf:
                        chunks.append(buf)
                    buf = part
            if buf:
                chunks.append(buf)
    return chunks


def speak_text(engine: pyttsx3.Engine, text: str) -> None:
    t = normalize_whitespace(text)
    if not t:
        return
    engine.say(t)
    engine.runAndWait()


def narrate_pages(doc: fitz.Document, engine: pyttsx3.Engine, start_page: int, end_page: int) -> None:
    page = start_page
    while page <= end_page:
        txt = page_text(doc, page).strip()

        print("\n" + "-" * 90)
        print(f"NARRATING PAGE {page + 1}/{len(doc)}   (range: {start_page + 1}-{end_page + 1})")
        print("-" * 90)

        if not txt:
            print("(No readable text on this page.)")
        else:
            chunks = chunk_text_for_speaking(txt)
            for chunk in chunks:
                speak_text(engine, chunk)
                time.sleep(0.03)

        print("\nControls: [Enter]=next page | b=back | r=repeat | g=go to page | q=stop")
        cmd = input("> ").strip().lower()

        if cmd == "":
            page += 1
        elif cmd == "b":
            page = max(start_page, page - 1)
        elif cmd == "r":
            continue
        elif cmd == "g":
            val = input(f"Go to page number (1-based, {start_page + 1}-{end_page + 1}): ").strip()
            if val.isdigit():
                target = int(val) - 1
                if start_page <= target <= end_page:
                    page = target
                else:
                    print("Out of range.")
            else:
                print("Invalid page number.")
        elif cmd == "q":
            break
        else:
            print("Unknown command. Moving to next page.")
            page += 1


def main():
    # List books
    pdfs = list_pdf_files(BOOKS_DIR)
    if not pdfs:
        print(f"No PDF files found in: {BOOKS_DIR}")
        sys.exit(1)

    print(f"\nBooks found in: {BOOKS_DIR}\n")
    for i, path in enumerate(pdfs, start=1):
        print(f"[{i}] {os.path.basename(path)}")

    book_idx = choose_from_list(pdfs, "\nSelect a book (number): ")
    pdf_path = pdfs[book_idx]
    print(f"\nOpening: {os.path.basename(pdf_path)}")

    doc = fitz.open(pdf_path)

    # Setup TTS
    engine = init_tts()
    print("\nTTS setup (optional):")
    print("  - Press Enter to accept defaults")
    print("  - Or type 'v' to list voices")
    cmd = input("Choice (Enter/v): ").strip().lower()
    if cmd == "v":
        list_voices(engine)
        v = input("Select voice index (Enter to skip): ").strip()
        if v.isdigit():
            set_voice(engine, int(v))

    rate_in = input("Speech rate (e.g., 160-220, Enter to keep default): ").strip()
    if rate_in.isdigit():
        set_rate(engine, int(rate_in))

    # Detect chapters
    print("\nDetecting chapters...")
    chapters = detect_chapters(doc)

    if not chapters:
        print("\nNo chapters detected automatically.")
        print("This usually happens if the PDF has no Table of Contents and no 'CHAPTER' headings, or it is scanned images.")
        print("Fallback: choose a page range to narrate.\n")

        sp = input(f"Start page (1-{len(doc)}): ").strip()
        ep = input(f"End page (1-{len(doc)}): ").strip()
        if not (sp.isdigit() and ep.isdigit()):
            print("Invalid page numbers.")
            doc.close()
            return

        start_page = max(0, min(int(sp) - 1, len(doc) - 1))
        end_page = max(0, min(int(ep) - 1, len(doc) - 1))
        if end_page < start_page:
            start_page, end_page = end_page, start_page

        narrate_pages(doc, engine, start_page, end_page)
        doc.close()
        return

    # Show detected chapters
    print("\nDetected chapters/sections:\n")
    for i, ch in enumerate(chapters, start=1):
        title = f" — {ch.title}" if ch.title else ""
        label = f"{ch.label} " if ch.label and ch.label != "TOC" else ""
        print(f"[{i}] {label}{title}".strip() + f"   (pages {ch.start_page + 1}-{ch.end_page + 1})")

    chap_idx = choose_from_list([c.title for c in chapters], "\nWhich chapter do you want to narrate? (number): ")
    ch = chapters[chap_idx]
    print("\nStarting narration:")
    print(f"  Pages: {ch.start_page + 1}-{ch.end_page + 1}")
    if ch.title:
        print(f"  Title: {ch.title}")

    narrate_pages(doc, engine, ch.start_page, ch.end_page)
    doc.close()


if __name__ == "__main__":
    main()
