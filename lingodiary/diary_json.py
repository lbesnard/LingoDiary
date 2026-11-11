"""
diary_json.py — JSON schema, serialisation helpers, and SM-2 SRS algorithm
for the LingoAnki diary database.

Schema (schema_version 1.3.0).  Entry ``index`` values are whatever was stored
(the generation pipeline historically used 0-based, the API 1-based); code must
never assume a base and should match entries by ``index`` as stored:
{
  "schema_version": "1.2.0",
  "diaries": [
    {
      "date": "2026/01/21",          # YYYY/MM/DD
      "title": "...",
      "lesson_audio_paths": {        # full lesson MP3, relative to output_dir
        "original": "TPRS/….mp3",
        "enhanced": "TPRS/…_enhanced.mp3"
      },
      "entries": [
        {
          "index": 1,
          "input_language_sentence": "...",
          "user_trial_translation": "...",
          "output_language_translation": "...",
          "tips": "...",
          "lessons": {
            "reviewing": {            # SRS state — shared across all variants
              "status": "new",        # "new" | "learning" | "mastered"
              "mastery_score": 0,     # 0-5 (last SM-2 quality rating)
              "last_reviewed": null,  # ISO-8601 UTC or null
              "next_review": null,    # ISO-8601 UTC or null
              "interval_days": 1,
              "review_count": 0
            },
            "original": {
              "sentence": "...",
              "sentence_audio_path": "TPRS/SEGMENTS/2026-01-21_original/0_s.mp3",
              "audio_timing": {"start_ms": 0, "end_ms": 0},
              "qa": [{
                "question": "...", "answer": "...",
                "question_input": "...",   # question translated to primary/input language
                "answer_input": "...",     # answer translated to primary/input language
                "question_audio_path": "TPRS/SEGMENTS/2026-01-21_original/0_q0.mp3",
                "answer_audio_path":   "TPRS/SEGMENTS/2026-01-21_original/0_a0.mp3",
                "question_timing": {"start_ms": 0, "end_ms": 0},
                "answer_timing":   {"start_ms": 0, "end_ms": 0}
              }]
            },
            "enhanced": {...},
            "present":  {...},
            "future":   {...}
          }
        }
      ]
    }
  ]
}
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = "1.3.0"
VARIANTS = ("original", "enhanced", "present", "future")


# ── Dataclasses ────────────────────────────────────────────────────────────────


@dataclass
class AudioTiming:
    start_ms: int = 0
    end_ms: int = 0

    def to_dict(self) -> dict:
        return {"start_ms": self.start_ms, "end_ms": self.end_ms}

    @classmethod
    def from_dict(cls, d: dict) -> "AudioTiming":
        if not isinstance(d, dict):
            return cls()
        return cls(
            start_ms=int(d.get("start_ms") or 0), end_ms=int(d.get("end_ms") or 0)
        )


@dataclass
class QA:
    question: str = ""
    answer: str = ""
    question_input: str = ""  # question translated to primary/input language
    answer_input: str = ""  # answer translated to primary/input language
    question_timing: AudioTiming = field(default_factory=AudioTiming)
    answer_timing: AudioTiming = field(default_factory=AudioTiming)
    question_audio_path: str = (
        ""  # relative to output_dir, e.g. "TPRS/SEGMENTS/2025-12-14_original/0_q0.mp3"
    )
    answer_audio_path: str = ""  # relative to output_dir
    question_input_language_audio_path: str = ""  # Input Language TTS for Drive Mode
    answer_input_language_audio_path: str = ""  # Input Language TTS for Drive Mode
    generated_at: Optional[str] = (
        None  # ISO-8601 UTC — when this Q&A's audio segments were generated
    )

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "question_input": self.question_input,
            "answer_input": self.answer_input,
            "question_timing": self.question_timing.to_dict(),
            "answer_timing": self.answer_timing.to_dict(),
            "question_audio_path": self.question_audio_path,
            "answer_audio_path": self.answer_audio_path,
            "question_input_language_audio_path": self.question_input_language_audio_path,
            "answer_input_language_audio_path": self.answer_input_language_audio_path,
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "QA":
        if not isinstance(d, dict):
            return cls()
        return cls(
            question=d.get("question", ""),
            answer=d.get("answer", ""),
            question_input=d.get("question_input", ""),
            answer_input=d.get("answer_input", ""),
            question_timing=AudioTiming.from_dict(d.get("question_timing", {})),
            answer_timing=AudioTiming.from_dict(d.get("answer_timing", {})),
            question_audio_path=d.get("question_audio_path", ""),
            answer_audio_path=d.get("answer_audio_path", ""),
            question_input_language_audio_path=d.get(
                "question_input_language_audio_path", ""
            ),
            answer_input_language_audio_path=d.get(
                "answer_input_language_audio_path", ""
            ),
            generated_at=d.get("generated_at"),
        )


@dataclass
class SentenceBlock:
    sentence: str = ""
    sentence_input: str = ""  # primary-language translation of this variant sentence
    audio_timing: AudioTiming = field(default_factory=AudioTiming)
    qa: list[QA] = field(default_factory=list)
    sentence_audio_path: str = (
        ""  # relative to output_dir, e.g. "TPRS/SEGMENTS/2025-12-14_original/0_s.mp3"
    )
    sentence_audio_generated_at: Optional[str] = (
        None  # ISO-8601 UTC — when this sentence's audio segment was generated
    )
    sentence_input_language_audio_path: str = ""  # Input Language TTS for Drive Mode

    def to_dict(self) -> dict:
        return {
            "sentence": self.sentence,
            "sentence_input": self.sentence_input,
            "audio_timing": self.audio_timing.to_dict(),
            "qa": [q.to_dict() for q in self.qa],
            "sentence_audio_path": self.sentence_audio_path,
            "sentence_audio_generated_at": self.sentence_audio_generated_at,
            "sentence_input_language_audio_path": self.sentence_input_language_audio_path,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SentenceBlock":
        if not isinstance(d, dict):
            return cls()
        return cls(
            sentence=d.get("sentence", ""),
            sentence_input=d.get("sentence_input", ""),
            audio_timing=AudioTiming.from_dict(d.get("audio_timing", {})),
            qa=[QA.from_dict(q) for q in (d.get("qa") or [])],
            sentence_audio_path=d.get("sentence_audio_path", ""),
            sentence_audio_generated_at=d.get("sentence_audio_generated_at"),
            sentence_input_language_audio_path=d.get(
                "sentence_input_language_audio_path", ""
            ),
        )


@dataclass
class ReviewingState:
    status: str = "new"  # "new" | "learning" | "mastered"
    mastery_score: int = 0  # 0-5
    last_reviewed: Optional[str] = None  # ISO-8601 UTC
    next_review: Optional[str] = None  # ISO-8601 UTC
    interval_days: int = 1
    review_count: int = 0

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "mastery_score": self.mastery_score,
            "last_reviewed": self.last_reviewed,
            "next_review": self.next_review,
            "interval_days": self.interval_days,
            "review_count": self.review_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewingState":
        if not isinstance(d, dict):
            return cls()
        return cls(
            status=d.get("status", "new"),
            mastery_score=d.get("mastery_score", 0),
            last_reviewed=d.get("last_reviewed"),
            next_review=d.get("next_review"),
            interval_days=d.get("interval_days", 1),
            review_count=d.get("review_count", 0),
        )


@dataclass
class VariantSet:
    reviewing: ReviewingState = field(default_factory=ReviewingState)
    original: SentenceBlock = field(default_factory=SentenceBlock)
    enhanced: SentenceBlock = field(default_factory=SentenceBlock)
    present: SentenceBlock = field(default_factory=SentenceBlock)
    future: SentenceBlock = field(default_factory=SentenceBlock)

    def to_dict(self) -> dict:
        return {
            "reviewing": self.reviewing.to_dict(),
            "original": self.original.to_dict(),
            "enhanced": self.enhanced.to_dict(),
            "present": self.present.to_dict(),
            "future": self.future.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "VariantSet":
        if not isinstance(d, dict):
            return cls()
        return cls(
            reviewing=ReviewingState.from_dict(d.get("reviewing", {})),
            original=SentenceBlock.from_dict(d.get("original", {})),
            enhanced=SentenceBlock.from_dict(d.get("enhanced", {})),
            present=SentenceBlock.from_dict(d.get("present", {})),
            future=SentenceBlock.from_dict(d.get("future", {})),
        )

    def get_variant(self, name: str) -> SentenceBlock:
        if name not in VARIANTS:
            return SentenceBlock()
        return getattr(self, name)

    def set_variant(self, name: str, variant: SentenceBlock) -> None:
        if name in VARIANTS:
            setattr(self, name, variant)


@dataclass
class Sentence:
    index: int = 0
    input_language_sentence: str = ""
    user_trial_translation: str = ""
    output_language_translation: str = ""
    tips: str = ""
    lessons: VariantSet = field(default_factory=VariantSet)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "input_language_sentence": self.input_language_sentence,
            "user_trial_translation": self.user_trial_translation,
            "output_language_translation": self.output_language_translation,
            "tips": self.tips,
            "lessons": self.lessons.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Sentence":
        if not isinstance(d, dict):
            return cls()
        return cls(
            index=d.get("index", 0),
            input_language_sentence=d.get("input_language_sentence", ""),
            user_trial_translation=d.get("user_trial_translation", ""),
            output_language_translation=d.get("output_language_translation", ""),
            tips=d.get("tips", ""),
            lessons=VariantSet.from_dict(d.get("lessons", {})),
        )


@dataclass
class DiaryDay:
    date: str = ""  # "YYYY/MM/DD"
    title: str = ""
    last_reviewed: Optional[str] = None  # ISO-8601 UTC when lesson was last played
    entries: list[Sentence] = field(default_factory=list)
    lesson_audio_paths: dict = field(
        default_factory=dict
    )  # variant → relative path to full lesson MP3
    lesson_mp3_timestamps: dict = field(
        default_factory=dict
    )  # variant → ISO-8601 UTC datetime when the full MP3 was last assembled

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "title": self.title,
            "last_reviewed": self.last_reviewed,
            "lesson_audio_paths": self.lesson_audio_paths,
            "lesson_mp3_timestamps": self.lesson_mp3_timestamps,
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DiaryDay":
        lap = d.get("lesson_audio_paths", {})
        lmt = d.get("lesson_mp3_timestamps", {})
        return cls(
            date=d.get("date", ""),
            title=d.get("title", ""),
            last_reviewed=d.get("last_reviewed"),
            entries=[Sentence.from_dict(e) for e in d.get("entries", [])],
            lesson_audio_paths=lap if isinstance(lap, dict) else {},
            lesson_mp3_timestamps=lmt if isinstance(lmt, dict) else {},
        )


@dataclass
class DiaryJson:
    schema_version: str = SCHEMA_VERSION
    diaries: list[DiaryDay] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "diaries": [d.to_dict() for d in self.diaries],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DiaryJson":
        return cls(
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            diaries=[DiaryDay.from_dict(day) for day in d.get("diaries", [])],
        )


# ── I/O helpers ────────────────────────────────────────────────────────────────


def load_diary_json(path: str | Path) -> DiaryJson:
    """Load diary JSON from disk. Returns an empty DiaryJson if file not found or empty."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return DiaryJson()
    with open(path, encoding="utf-8") as f:
        return DiaryJson.from_dict(json.load(f))


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


@contextmanager
def diary_lock(path: str | Path):
    """Process-wide re-entrant lock for one diary.json.

    Hold it around any load -> modify -> save sequence so concurrent API
    requests and the background worker cannot overwrite each other.
    """
    key = os.path.realpath(str(path))
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield


def _merge_user_state(diary: DiaryJson, on_disk: DiaryJson) -> None:
    """Copy fields mutated by API requests (SRS state, last_reviewed, trials)
    from the freshly loaded on-disk diary into *diary*.

    Long-running background jobs hold an in-memory copy for minutes; without
    this, saving it would silently revert anything the user did meanwhile.
    """
    disk_days = {d.date: d for d in on_disk.diaries}
    mem_dates = {d.date for d in diary.diaries}
    # Days created via the API while the job was running: keep them.
    for disk_day in on_disk.diaries:
        if disk_day.date not in mem_dates:
            diary.diaries.append(disk_day)
    for day in diary.diaries:
        disk_day = disk_days.get(day.date)
        if disk_day is None or disk_day is day:
            continue
        # Sentences appended via the API while the job was running: keep them.
        mem_indexes = {e.index for e in day.entries}
        mem_texts = {e.input_language_sentence for e in day.entries}
        added = [
            e
            for e in disk_day.entries
            if e.index not in mem_indexes
            and e.input_language_sentence not in mem_texts
        ]
        if added:
            day.entries.extend(added)
            day.entries.sort(key=lambda e: e.index)
        if disk_day.last_reviewed:
            day.last_reviewed = disk_day.last_reviewed
        disk_entries = {e.index: e for e in disk_day.entries}
        for entry in day.entries:
            disk_entry = disk_entries.get(entry.index)
            if disk_entry is None:
                continue
            if (
                disk_entry.input_language_sentence
                and disk_entry.input_language_sentence != entry.input_language_sentence
            ):
                continue  # different sentence now lives at this index
            entry.lessons.reviewing = disk_entry.lessons.reviewing
            if disk_entry.user_trial_translation:
                entry.user_trial_translation = disk_entry.user_trial_translation


def save_diary_json(
    diary: DiaryJson, path: str | Path, preserve_user_state: bool = False
) -> None:
    """Atomically write diary JSON to disk (write-to-tmp then rename).

    Pass ``preserve_user_state=True`` from background jobs that work on a
    long-lived in-memory copy: SRS state, ``last_reviewed`` and translation
    attempts are re-read from disk under the lock and kept.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with diary_lock(path):
        if preserve_user_state and path.exists() and path.stat().st_size > 0:
            try:
                _merge_user_state(diary, load_diary_json(path))
            except (OSError, ValueError):
                pass

        # ── Atomic write ──────────────────────────────────────────────────────
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(diary.to_dict(), f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


# ── Query helpers ──────────────────────────────────────────────────────────────


def get_day(diary: DiaryJson, date_str: str) -> Optional[DiaryDay]:
    """Return the DiaryDay for date_str ("YYYY/MM/DD") or None."""
    for day in diary.diaries:
        if day.date == date_str:
            return day
    return None


def get_entry(diary: DiaryJson, date_str: str, index: int) -> Optional[Sentence]:
    """Return a specific entry by date and index, or None."""
    day = get_day(diary, date_str)
    if day is None:
        return None
    for entry in day.entries:
        if entry.index == index:
            return entry
    return None


def upsert_day(
    diary: DiaryJson, date_str: str, title: str, entries: list[Sentence]
) -> DiaryJson:
    """Insert or update the DiaryDay for the given date.

    If the day exists it is mutated in place.  Entries are matched by ``index``;
    for a match whose sentence text is unchanged, SRS state and variants are
    preserved, and text fields left empty in the new entry (translation, tips,
    trial) keep their old values.  Existing entries that are absent from
    *entries* are kept — this function never deletes sentences.
    """
    existing_day = get_day(diary, date_str)

    if existing_day is None:
        diary.diaries.append(DiaryDay(date=date_str, title=title, entries=entries))
        diary.diaries.sort(key=lambda d: d.date, reverse=True)
        return diary

    if title:
        existing_day.title = title

    old_by_index = {e.index: e for e in existing_day.entries}
    merged: dict[int, Sentence] = dict(old_by_index)
    for new_entry in entries:
        old = old_by_index.get(new_entry.index)
        if old is not None and (
            old.input_language_sentence.strip()
            == new_entry.input_language_sentence.strip()
        ):
            new_entry.lessons = old.lessons
            for attr in (
                "output_language_translation",
                "tips",
                "user_trial_translation",
            ):
                if not getattr(new_entry, attr):
                    setattr(new_entry, attr, getattr(old, attr))
        merged[new_entry.index] = new_entry

    existing_day.entries = [merged[i] for i in sorted(merged)]
    return diary


def upsert_variant(
    diary: DiaryJson,
    date_str: str,
    entry_index: int,
    variant_name: str,
    variant: SentenceBlock,
) -> DiaryJson:
    """Update a specific variant's content for an existing entry."""
    entry = get_entry(diary, date_str, entry_index)
    if entry is not None:
        entry.lessons.set_variant(variant_name, variant)
    return diary


# ── SM-2 SRS algorithm ─────────────────────────────────────────────────────────


def apply_sm2(
    reviewing: ReviewingState, score: int, now: Optional[datetime] = None
) -> ReviewingState:
    """
    Apply a simplified SM-2 update to a ReviewingState.

    Score mapping:
        0 → Again: reset to 1-day interval, status = learning
        2 → Hard:  interval * 1.2 (min 1), status = learning
        3 → Good:  interval * 2.5 (min 3 on first review), status = learning
        5 → Easy:  interval * 4.0 (min 7 on first review),
                   status = mastered if review_count >= 2 else learning

    Returns the mutated ReviewingState (same object).
    """
    now_utc = now or datetime.now(timezone.utc)
    reviewing.review_count += 1
    reviewing.last_reviewed = now_utc.isoformat()
    reviewing.mastery_score = score

    if score == 0:  # Again
        reviewing.interval_days = 1
        reviewing.status = "learning"
    elif score == 2:  # Hard
        reviewing.interval_days = max(1, round(reviewing.interval_days * 1.2))
        reviewing.status = "learning"
    elif score == 3:  # Good
        reviewing.interval_days = (
            round(reviewing.interval_days * 2.5) if reviewing.interval_days > 1 else 3
        )
        reviewing.status = "learning"
    elif score == 5:  # Easy
        reviewing.interval_days = (
            round(reviewing.interval_days * 4.0) if reviewing.interval_days > 1 else 7
        )
        reviewing.status = "mastered" if reviewing.review_count >= 2 else "learning"
    else:
        raise ValueError(f"Invalid SM-2 score {score!r}. Valid scores: 0, 2, 3, 5.")

    reviewing.next_review = (
        now_utc + timedelta(days=reviewing.interval_days)
    ).isoformat()
    return reviewing


def update_srs(
    diary: DiaryJson,
    date_str: str,
    entry_index: int,
    score: int,
    now: Optional[datetime] = None,
) -> tuple[DiaryJson, ReviewingState]:
    """
    Apply SM-2 to the reviewing state of a specific entry.

    Returns (updated_diary, updated_reviewing_state).
    Raises ValueError if the entry does not exist.
    """
    entry = get_entry(diary, date_str, entry_index)
    if entry is None:
        raise ValueError(f"No entry found for date={date_str} index={entry_index}")
    apply_sm2(entry.lessons.reviewing, score, now=now)
    return diary, entry.lessons.reviewing


# ── Statistics helpers ─────────────────────────────────────────────────────────


def compute_stats(diary: DiaryJson) -> dict:
    """
    Return entry-level SRS statistics across all diary days.

    Returns:
        {"total": int, "new": int, "learning": int, "mastered": int}
    """
    counts = {"total": 0, "new": 0, "learning": 0, "mastered": 0}
    for day in diary.diaries:
        for entry in day.entries:
            counts["total"] += 1
            status = entry.lessons.reviewing.status
            if status in counts:
                counts[status] += 1
            else:
                counts["new"] += 1
    return counts


def get_due_entries(diary: DiaryJson) -> list[tuple[DiaryDay, Sentence]]:
    """
    Return (day, entry) pairs where next_review <= now or status == "new",
    sorted with overdue first, then new entries in date order.
    """
    now_utc = datetime.now(timezone.utc)
    overdue: list[tuple[DiaryDay, Sentence]] = []
    new_entries: list[tuple[DiaryDay, Sentence]] = []

    for day in sorted(diary.diaries, key=lambda d: d.date):
        for entry in day.entries:
            r = entry.lessons.reviewing
            if r.status == "new":
                new_entries.append((day, entry))
            elif r.next_review is not None:
                try:
                    due = datetime.fromisoformat(r.next_review)
                    if due.tzinfo is None:
                        due = due.replace(tzinfo=timezone.utc)
                    if due <= now_utc:
                        overdue.append((day, entry))
                except ValueError:
                    pass

    def _due_key(pair: tuple[DiaryDay, Sentence]) -> str:
        return pair[1].lessons.reviewing.next_review or ""

    overdue.sort(key=_due_key)  # most overdue first
    return overdue + new_entries


def get_recently_reviewed(diary: DiaryJson, limit: int = 5) -> list[DiaryDay]:
    """
    Return the N most recently reviewed days (days where at least one entry
    has been reviewed), sorted by last_reviewed DESC.
    """
    day_last_reviewed: list[tuple[str, DiaryDay]] = []
    for day in diary.diaries:
        latest = None
        for entry in day.entries:
            lr = entry.lessons.reviewing.last_reviewed
            if lr and (latest is None or lr > latest):
                latest = lr
        if latest:
            day_last_reviewed.append((latest, day))

    day_last_reviewed.sort(key=lambda x: x[0], reverse=True)
    return [day for _, day in day_last_reviewed[:limit]]
