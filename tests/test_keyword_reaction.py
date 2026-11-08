from hashlib import sha256
from pathlib import Path

import pytest

from pwcs2_bot.keyword_reaction import (
    is_xh_reaction_trigger,
    xh_reaction_image_path,
)


@pytest.mark.parametrize(
    "text",
    (
        "xh",
        "XHNB",
        "这把xh了",
        "又输了（xhnb）",
        "xh!",
    ),
)
def test_xh_reaction_matches_standalone_keywords(text: str) -> None:
    assert is_xh_reaction_trigger(text)


@pytest.mark.parametrize(
    "text",
    (
        "xhtml",
        "axh",
        "xhnb2",
        "_xh",
        "#ai xh",
        "/AI XHNB",
        "看看 https://example.com/xh",
        "今晚开黑",
    ),
)
def test_xh_reaction_avoids_embedded_words_and_explicit_ai(text: str) -> None:
    assert not is_xh_reaction_trigger(text)


def test_xh_reaction_image_is_packaged_png() -> None:
    path = xh_reaction_image_path()
    data = path.read_bytes()

    assert isinstance(path, Path)
    assert path.is_file()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert sha256(data).hexdigest() == (
        "f072828222f4eb07b51b4b1055d0d70a4cc3a3816d7d9fefa7307b7da02fc557"
    )
