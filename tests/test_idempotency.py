import time

from app.idempotency import COMPLETED, DEAL_CREATED, Ledger, candidate_keys, make_key


def test_key_ignores_case_and_whitespace():
    a = make_key("Maria@Example.com", "Need a  COI\nplease", 10)
    b = make_key("maria@example.com", "need a coi please ", 10)
    assert a == b


def test_key_changes_with_bucket_and_content():
    base = make_key("a@b.com", "hello", 10)
    assert base != make_key("a@b.com", "hello", 11)
    assert base != make_key("a@b.com", "hello there", 10)
    assert base != make_key("c@b.com", "hello", 10)


def test_candidate_keys_cover_previous_bucket():
    # 10:59:59 and 11:00:01 land in different buckets but must still match.
    before = candidate_keys("a@b.com", "hi", 3600 * 10 + 3599, 3600)
    after = candidate_keys("a@b.com", "hi", 3600 * 11 + 1, 3600)
    assert before[0] in after


def test_claim_lifecycle(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    keys = ["k-now", "k-prev"]

    assert ledger.claim("k-now", keys) is None  # claimed
    assert ledger.claim("k-now", keys).status == "processing"  # concurrent duplicate

    ledger.mark_deal_created("k-now", "c1", "d1", '{"x": 1}')
    entry = ledger.claim("k-now", keys)
    assert (entry.status, entry.deal_id) == (DEAL_CREATED, "d1")

    ledger.complete("k-now", note_id="n1")
    entry = ledger.claim("k-now", keys)
    assert (entry.status, entry.contact_id, entry.note_id) == (COMPLETED, "c1", "n1")


def test_previous_bucket_entry_blocks_new_claim(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.claim("k-prev", ["k-prev"])
    ledger.complete("k-prev", deal_id="d1")
    assert ledger.claim("k-now", ["k-now", "k-prev"]).deal_id == "d1"


def test_release_allows_reclaim(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.claim("k", ["k"])
    ledger.release("k")
    assert ledger.claim("k", ["k"]) is None


def test_stale_processing_claim_is_reclaimed(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"), stale_after_seconds=0.01)
    ledger.claim("k", ["k"])
    time.sleep(0.02)
    assert ledger.claim("k", ["k"]) is None
