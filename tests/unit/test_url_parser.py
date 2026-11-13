"""Unit tests for gdown2.domain.url_parser."""

from __future__ import annotations

import pytest

from gdown2.domain.errors import InvalidUrlError
from gdown2.domain.url_parser import TargetKind, extract_id, parse_target


@pytest.mark.parametrize(
    ("raw_input", "expected_id", "expected_kind"),
    [
        (
            "https://drive.google.com/file/d/1A2B3C4D5E6F7G8H9I0J/view?usp=sharing",
            "1A2B3C4D5E6F7G8H9I0J",
            TargetKind.FILE,
        ),
        (
            "https://drive.google.com/drive/u/0/folders/1FolderId_9876543210?usp=drive_link",
            "1FolderId_9876543210",
            TargetKind.FOLDER,
        ),
        (
            "https://drive.google.com/open?id=1OpenQueryId_12345678",
            "1OpenQueryId_12345678",
            TargetKind.FILE,
        ),
        (
            "https://drive.google.com/uc?export=download&id=1UcExportId_12345678",
            "1UcExportId_12345678",
            TargetKind.FILE,
        ),
        (
            "https://drive.usercontent.google.com/download?id=1UserContent_12345678&confirm=t",
            "1UserContent_12345678",
            TargetKind.FILE,
        ),
        (
            "https://docs.google.com/document/d/1DocId_1234567890abc/edit",
            "1DocId_1234567890abc",
            TargetKind.DOCUMENT,
        ),
        (
            "https://docs.google.com/spreadsheets/d/1SheetId_1234567890a/edit#gid=0",
            "1SheetId_1234567890a",
            TargetKind.SPREADSHEET,
        ),
        (
            "https://docs.google.com/presentation/d/1SlideId_1234567890a/edit",
            "1SlideId_1234567890a",
            TargetKind.PRESENTATION,
        ),
        (
            "1BareDriveId_1234567890",
            "1BareDriveId_1234567890",
            TargetKind.UNKNOWN,
        ),
    ],
)
def test_parse_valid_targets(
    raw_input: str,
    expected_id: str,
    expected_kind: TargetKind,
) -> None:
    target = parse_target(raw_input)
    assert target.id == expected_id
    assert target.kind == expected_kind
    assert extract_id(raw_input) == expected_id


def test_parse_resource_key_and_format() -> None:
    url = (
        "https://drive.google.com/drive/folders/1FolderKey_123456789"
        "?resourcekey=0-abcDEF123&format=pdf"
    )
    target = parse_target(url)
    assert target.id == "1FolderKey_123456789"
    assert target.is_folder_hint
    assert target.resource_key == "0-abcDEF123"
    assert target.export_format == "pdf"


@pytest.mark.parametrize(
    "bad_input",
    [
        "",
        "   ",
        "short",
        "ftp://drive.google.com/file/d/1A2B3C4D5E6F7G8H9I0J/view",
        "https://evil.example.com/file/d/1A2B3C4D5E6F7G8H9I0J/view",
        "https://drive.google.com/drive/my-drive",
    ],
)
def test_parse_invalid_targets_raise(bad_input: str) -> None:
    with pytest.raises(InvalidUrlError):
        parse_target(bad_input)

