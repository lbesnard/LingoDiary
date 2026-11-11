"""Tests for lingodiary/audio_timing.py.

Covers pure logic only — no TTS plugin, no pydub I/O, no OpenAI.
Segment durations are injected via unittest.mock so tests run offline
and complete in milliseconds.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from lingodiary.audio_timing import (
    _recompute_timings_from_segments,
    _segments_complete,
)
from lingodiary.diary_json import (
    AudioTiming,
    DiaryDay,
    DiaryJson,
    QA,
    Sentence,
    SentenceBlock,
    VariantSet,
)

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_sentence(sentence_text="Hello", qa_count=1) -> Sentence:
    """Build a minimal Sentence with one VariantSet (original only)."""
    qa = [
        QA(
            question=f"Q{i}?",
            answer=f"A{i}.",
            question_audio_path=f"TPRS/SEGMENTS/2026-01-01_original/0_q{i}.mp3",
            answer_audio_path=f"TPRS/SEGMENTS/2026-01-01_original/0_a{i}.mp3",
            generated_at="2026-01-01T00:00:00+00:00",
        )
        for i in range(qa_count)
    ]
    block = SentenceBlock(
        sentence=sentence_text,
        sentence_audio_path="TPRS/SEGMENTS/2026-01-01_original/0_s.mp3",
        sentence_audio_generated_at="2026-01-01T00:00:00+00:00",
        qa=qa,
    )
    vs = VariantSet(original=block)
    return Sentence(index=1, input_language_sentence="Hello", lessons=vs)


def _make_day(entries=None, mp3_ts=None) -> DiaryDay:
    day = DiaryDay(date="2026/01/01")
    day.entries = entries or [_make_sentence()]
    if mp3_ts:
        day.lesson_mp3_timestamps["original"] = mp3_ts
    return day


# ── _segments_complete ─────────────────────────────────────────────────────────


class TestSegmentsComplete:
    def test_all_files_present_returns_true(self, tmp_path):
        """All expected segment files exist → complete."""
        segs_dir = str(tmp_path / "segs")
        os.makedirs(segs_dir)
        # Create expected files for 1 entry with 1 QA pair
        (tmp_path / "segs" / "0_s.mp3").touch()
        (tmp_path / "segs" / "0_q0.mp3").touch()
        (tmp_path / "segs" / "0_a0.mp3").touch()

        entries = [_make_sentence(qa_count=1)]
        assert _segments_complete(segs_dir, entries, "original") is True

    def test_missing_sentence_segment_returns_false(self, tmp_path):
        """Sentence segment missing → not complete."""
        segs_dir = str(tmp_path / "segs")
        os.makedirs(segs_dir)
        # No files created
        entries = [_make_sentence(qa_count=1)]
        assert _segments_complete(segs_dir, entries, "original") is False

    def test_missing_qa_segment_returns_false(self, tmp_path):
        """Q segment present but A segment missing → not complete."""
        segs_dir = str(tmp_path / "segs")
        os.makedirs(segs_dir)
        (tmp_path / "segs" / "0_s.mp3").touch()
        (tmp_path / "segs" / "0_q0.mp3").touch()
        # 0_a0.mp3 deliberately absent
        entries = [_make_sentence(qa_count=1)]
        assert _segments_complete(segs_dir, entries, "original") is False

    def test_empty_entries_returns_true(self, tmp_path):
        """No entries → nothing to check, vacuously complete."""
        assert _segments_complete(str(tmp_path), [], "original") is True

    def test_missing_dir_returns_false(self, tmp_path):
        """Non-existent segment directory → not complete."""
        assert (
            _segments_complete(
                str(tmp_path / "nonexistent"), [_make_sentence()], "original"
            )
            is False
        )


# ── _recompute_timings_from_segments ──────────────────────────────────────────


class TestRecomputeTimingsFromSegments:
    """
    Patches pydub.AudioSegment.from_mp3 so tests don't need real MP3 files.
    The mock returns a MagicMock whose len() equals the injected duration_ms.
    """

    def _mock_audio(self, duration_ms: int):
        """Return a MagicMock that behaves like an AudioSegment of duration_ms."""
        mock = MagicMock()
        mock.__len__ = MagicMock(return_value=duration_ms)
        return mock

    @patch("pydub.AudioSegment")
    def test_single_entry_no_qa(self, MockAudioSegment, tmp_path):
        """One sentence, no Q&A → timing is [0, s_dur*R]."""
        s_dur = 1000  # 1 second
        MockAudioSegment.from_mp3.return_value = self._mock_audio(s_dur)

        segs_dir = str(tmp_path / "segs")
        os.makedirs(segs_dir)
        (tmp_path / "segs" / "0_s.mp3").touch()

        entry = _make_sentence(qa_count=0)
        R = 1
        pause_ms = 500
        answer_silence_ms = 2000

        result = _recompute_timings_from_segments(
            entries=[entry],
            variant_key="original",
            segs_dir=segs_dir,
            repeat_tprs=R,
            pause_ms=pause_ms,
            answer_silence_ms=answer_silence_ms,
            output_dir=str(tmp_path),
        )

        assert result == [(0, s_dur)]
        assert entry.lessons.original.audio_timing.start_ms == 0
        assert entry.lessons.original.audio_timing.end_ms == s_dur

    @patch("pydub.AudioSegment")
    def test_single_entry_with_one_qa(self, MockAudioSegment, tmp_path):
        """One sentence + one Q&A → timings are sequential and non-overlapping."""
        s_dur = 1000
        q_dur = 600
        a_dur = 800
        pause_ms = 500
        answer_silence_ms = 2000
        R = 1

        def mock_from_mp3(path):
            name = os.path.basename(path)
            if name.endswith("_s.mp3"):
                return self._mock_audio(s_dur)
            elif "_q" in name:
                return self._mock_audio(q_dur)
            elif "_a" in name:
                return self._mock_audio(a_dur)
            return self._mock_audio(0)

        MockAudioSegment.from_mp3.side_effect = mock_from_mp3

        segs_dir = str(tmp_path / "segs")
        os.makedirs(segs_dir)
        for name in ("0_s.mp3", "0_q0.mp3", "0_a0.mp3"):
            (tmp_path / "segs" / name).touch()

        entry = _make_sentence(qa_count=1)
        result = _recompute_timings_from_segments(
            entries=[entry],
            variant_key="original",
            segs_dir=segs_dir,
            repeat_tprs=R,
            pause_ms=pause_ms,
            answer_silence_ms=answer_silence_ms,
            output_dir=str(tmp_path),
        )

        # Sentence: 0 → 1000
        assert result == [(0, s_dur)]
        # Q starts at: 1000 (end) + 500 (pause) + 500 (pause before Q) = 2000
        expected_q_start = s_dur + pause_ms + pause_ms
        expected_q_end = expected_q_start + q_dur
        qa = entry.lessons.original.qa[0]
        assert qa.question_timing.start_ms == expected_q_start
        assert qa.question_timing.end_ms == expected_q_end
        # A starts at: q_end + answer_silence_ms
        expected_a_start = expected_q_end + answer_silence_ms
        expected_a_end = expected_a_start + a_dur
        assert qa.answer_timing.start_ms == expected_a_start
        assert qa.answer_timing.end_ms == expected_a_end

    @patch("pydub.AudioSegment")
    def test_missing_sentence_segment_skipped(self, MockAudioSegment, tmp_path):
        """Entry with no sentence segment → (0,0) timing, cumulative not advanced."""
        MockAudioSegment.from_mp3.return_value = self._mock_audio(1000)

        segs_dir = str(tmp_path / "segs")
        os.makedirs(segs_dir)
        # No 0_s.mp3 created

        entry = _make_sentence(qa_count=0)
        result = _recompute_timings_from_segments(
            entries=[entry],
            variant_key="original",
            segs_dir=segs_dir,
            repeat_tprs=1,
            pause_ms=500,
            answer_silence_ms=2000,
            output_dir=str(tmp_path),
        )

        # No segments found → returns []
        assert result == []

    @patch("pydub.AudioSegment")
    def test_missing_qa_segment_does_not_shift_subsequent_timings(
        self, MockAudioSegment, tmp_path
    ):
        """Missing Q segment → warning, cumulative NOT advanced by Q duration.

        This is the bug fixed in cd1e7c4: previously missing segments silently
        caused downstream timing drift.
        """
        s_dur = 1000
        a_dur = 800
        pause_ms = 500
        answer_silence_ms = 2000
        R = 1

        def mock_from_mp3(path):
            if path.endswith("_s.mp3"):
                return self._mock_audio(s_dur)
            elif "_a" in path:
                return self._mock_audio(a_dur)
            return self._mock_audio(0)

        MockAudioSegment.from_mp3.side_effect = mock_from_mp3

        segs_dir = str(tmp_path / "segs")
        os.makedirs(segs_dir)
        (tmp_path / "segs" / "0_s.mp3").touch()
        # 0_q0.mp3 deliberately absent — only 0_a0.mp3 present
        (tmp_path / "segs" / "0_a0.mp3").touch()

        entry = _make_sentence(qa_count=1)
        _recompute_timings_from_segments(
            entries=[entry],
            variant_key="original",
            segs_dir=segs_dir,
            repeat_tprs=R,
            pause_ms=pause_ms,
            answer_silence_ms=answer_silence_ms,
            output_dir=str(tmp_path),
        )

        qa = entry.lessons.original.qa[0]
        # Q timing should be zeroed (not set), Q audio path empty
        assert qa.question_timing.start_ms == 0
        assert qa.question_timing.end_ms == 0

        # A timing should still be set correctly despite missing Q
        # cumulative after sentence: s_dur + pause_ms = 1500
        # pause before Q: +500 → 2000
        # Q missing — cumulative stays at 2000 (NOT advanced by q_dur)
        # answer_silence: +2000 → 4000
        assert (
            qa.answer_timing.start_ms == s_dur + pause_ms + pause_ms + answer_silence_ms
        )
        assert qa.answer_timing.end_ms == (
            s_dur + pause_ms + pause_ms + answer_silence_ms + a_dur
        )

    @patch("pydub.AudioSegment")
    def test_repeat_tprs_multiplies_duration(self, MockAudioSegment, tmp_path):
        """repeat_tprs=2 → sentence end_ms = s_dur * 2."""
        s_dur = 1000
        MockAudioSegment.from_mp3.return_value = self._mock_audio(s_dur)

        segs_dir = str(tmp_path / "segs")
        os.makedirs(segs_dir)
        (tmp_path / "segs" / "0_s.mp3").touch()

        entry = _make_sentence(qa_count=0)
        result = _recompute_timings_from_segments(
            entries=[entry],
            variant_key="original",
            segs_dir=segs_dir,
            repeat_tprs=2,
            pause_ms=0,
            answer_silence_ms=0,
            output_dir=str(tmp_path),
        )

        assert result == [(0, s_dur * 2)]

    @patch("pydub.AudioSegment")
    def test_two_entries_cumulative_is_sequential(self, MockAudioSegment, tmp_path):
        """Two sentences — second entry starts after first ends + pause."""
        s_dur = 1000
        pause_ms = 500
        MockAudioSegment.from_mp3.return_value = self._mock_audio(s_dur)

        segs_dir = str(tmp_path / "segs")
        os.makedirs(segs_dir)
        (tmp_path / "segs" / "0_s.mp3").touch()
        (tmp_path / "segs" / "1_s.mp3").touch()

        entry0 = _make_sentence(qa_count=0)
        entry1 = _make_sentence(qa_count=0)
        result = _recompute_timings_from_segments(
            entries=[entry0, entry1],
            variant_key="original",
            segs_dir=segs_dir,
            repeat_tprs=1,
            pause_ms=pause_ms,
            answer_silence_ms=2000,
            output_dir=str(tmp_path),
        )

        assert result[0] == (0, s_dur)
        assert result[1] == (s_dur + pause_ms, s_dur + pause_ms + s_dur)


# ── segment cache invalidation ─────────────────────────────────────────────────


class TestSegmentCacheInvalidation:
    def _entries(self, text):
        from lingodiary.diary_json import Sentence

        e = Sentence(index=1)
        e.lessons.original.sentence = text
        return [e]

    def test_changed_text_invalidates_cached_segments(self, tmp_path):
        from lingodiary import audio_timing as at

        segs = tmp_path
        (segs / "0_s.mp3").write_bytes(b"x")
        at._save_manifest(str(segs), {"0_s.mp3": at._text_hash("old text")})
        assert at._segments_complete(str(segs), self._entries("old text"), "original")
        assert not at._segments_complete(
            str(segs), self._entries("new text"), "original"
        )

    def test_legacy_segments_without_manifest_are_trusted(self, tmp_path):
        from lingodiary import audio_timing as at

        (tmp_path / "0_s.mp3").write_bytes(b"x")
        assert at._segments_complete(
            str(tmp_path), self._entries("anything"), "original"
        )

    def test_setup_logging_exists_for_cli(self):
        from lingodiary import audio_timing as at

        assert callable(at._setup_logging)
