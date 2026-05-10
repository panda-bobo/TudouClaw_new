import json
from app.domain_expert.corpus.source_flk_npc import iter_from_fixture, FlkDocument


def test_iter_from_fixture(tmp_path):
    fixture = tmp_path / "fixture.jsonl"
    docs = [
        {"title": "民法典", "url": "u1", "text": "test text",
         "metadata": {"law_name": "民法典", "source": "flk_npc"}},
        {"title": "刑法", "url": "u2", "text": "another",
         "metadata": {"law_name": "刑法", "source": "flk_npc"}},
    ]
    with open(fixture, "w") as f:
        for d in docs:
            f.write(json.dumps(d) + "\n")
    items = list(iter_from_fixture(str(fixture)))
    assert len(items) == 2
    assert items[0].title == "民法典"
    assert items[1].metadata["law_name"] == "刑法"
