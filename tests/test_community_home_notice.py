import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/send-community-home-notice.py"
SPEC = importlib.util.spec_from_file_location("community_home_notice", SCRIPT)
notice = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notice)


def test_scoped_notice_without_force_confirmation():
    params = notice.request_params()
    assert params["group_id"] == "100000021"
    assert params["confirm_required"] == params["pinned"] == params["tip_window_type"] == 0
    assert "https://192.0.2.10/niuniu/" in params["content"]
    assert "http://" not in params["content"]
    assert "image" not in params


def test_duplicate_matching_requires_exact_text_and_own_sender():
    item = {"notice_id": "id1", "sender_id": "bot", "message": {"text": notice.NOTICE}}
    assert notice.find_notice([item], "bot") is item
    assert notice.find_notice([item], "other") is None
    assert notice.find_notice([{**item, "message": {"text": "different"}}], "bot") is None
    with pytest.raises(ValueError):
        notice.find_notice({}, "bot")


def test_qq_html_entities_are_normalized_for_confirmation_and_deduplication():
    encoded = notice.NOTICE.replace("\n", "&#10;").replace(" ", "&nbsp;")
    item = {"notice_id": "id1", "sender_id": "bot", "message": {"text": encoded}}
    assert notice.find_notice([item], "bot") is item
    item["message"]["text"] += "changed"
    assert notice.find_notice([item], "bot") is None


@pytest.mark.parametrize("data", [None, [], [{"notice_id": "id1"}]])
def test_api_preserves_list_and_null(data):
    class Socket:
        def send(self, text):
            self.echo = json.loads(text)["echo"]

        def recv(self, **kwargs):
            return json.dumps({"echo": self.echo, "status": "ok", "retcode": 0, "data": data})

    assert notice.call(Socket(), "_get_group_notice", {}) == data
